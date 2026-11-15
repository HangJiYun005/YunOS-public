"""Immutable approval and human-authorization contracts for one real import.

The approval package always keeps production execution disabled.  A separate
HumanAuthorizationRecord may authorize exactly one execution after the user
has seen the final package digest.  This module validates documents only; it
never opens a database or writes an asset.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re
from typing import Any, Mapping

from memory.asset_baseline import (
    ASSET_BASELINE_SCHEMA_VERSION,
    AssetBaselineError,
    validate_asset_baseline,
)
from memory.ingestion_core import canonical_json_bytes, sanitize_reason_code


PRODUCTION_EXECUTION_PROTOCOL_VERSION = (
    "v8.3-real-chatgpt-import-production-execution-2"
)
APPROVAL_PACKAGE_SCHEMA_VERSION = "v8.3-real-import-approval-package-3"
AUTHORIZATION_SCHEMA_VERSION = "v8.3-real-import-human-authorization-2"
AUTHORIZATION_SCOPE = "v8.3_real_chatgpt_import"
STANDING_AUTHORIZATION_TEXT = "以后默认授权"
STANDING_AUTHORIZATION_TEXT_DIGEST = hashlib.sha256(
    STANDING_AUTHORIZATION_TEXT.encode("utf-8")
).hexdigest()
APPROVAL_STATUS = "awaiting_explicit_human_authorization"
PRODUCTION_DATABASE_RELATIVE_PATH = "data/ayunos_chatgpt.db"
PRODUCTION_ASSET_ROOT_RELATIVE_PATH = "data/ingestion_assets"
EXECUTION_ENTRY = "tools/run_authorized_real_import.py"
EXECUTION_STATE_LEDGER_RELATIVE_PATH = (
    "output/v8.3-real-import-asset-baseline-fix/execution_state.jsonl"
)
MAX_DOCUMENT_BYTES = 8 * 1024 * 1024

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
_IDENTITY_RE = re.compile(r"[a-z][a-z0-9_.:-]{7,191}\Z")

_APPROVAL_FIELDS = frozenset({
    "package_schema_version",
    "status",
    "production_execution_protocol_version",
    "created_at",
    "approval_expires_at",
    "code_commit",
    "production_main_baseline",
    "production_asset_baseline",
    "imports_baseline",
    "candidate_archive",
    "source_manifest_digest",
    "adapter_version",
    "exact_import_plan_digest",
    "one_time_plan_id",
    "before_counts",
    "after_counts",
    "new_message_count",
    "exact_duplicate_count",
    "conflict_counts",
    "old_time_new_id_count",
    "asset_classification",
    "asset_counts",
    "staged_byte_forecast",
    "backup",
    "clone_execution",
    "replay_result",
    "failure_and_recovery",
    "rollback_procedure",
    "maximum_expected_downtime",
    "operator_checklist",
    "execution_contract",
    "production_execution_authorized",
    "human_approval_required",
    "human_authorization_record_generated",
    "conflict_auto_decision",
    "historical_backfill",
    "v8302",
    "production_writer_persistent_enablement",
    "supersedes",
    "final_conclusion",
    "package_digest",
})

_AUTHORIZATION_FIELDS = frozenset({
    "authorization_id",
    "authorization_scope",
    "authorization_basis",
    "authorization_bound_by",
    "standing_authorization_text_digest",
    "approval_package_digest",
    "bound_approval_package_digest",
    "exact_import_plan_digest",
    "bound_import_plan_digest",
    "one_time_plan_id",
    "source_database_sha256",
    "source_schema_fingerprint",
    "source_archive_sha256",
    "source_manifest_digest",
    "bound_source_manifest_digest",
    "execution_contract_digest",
    "code_commit",
    "bound_code_commit",
    "imports_tree_sha256",
    "asset_baseline_digest",
    "backup_manifest_digest",
    "backup_set_digest",
    "user_authorization_text_digest",
    "authorized_at",
    "expires_at",
    "one_time_execution_id",
    "authorization_schema_version",
    "authorization_digest",
})

_EXECUTION_CONTRACT_FIELDS = frozenset({
    "execution_entry",
    "authorization_schema_version",
    "authorization_scope",
    "asset_baseline_schema_version",
    "production_database_relative_path",
    "production_asset_root_relative_path",
    "asset_storage_layout",
    "service_quiescence_required",
    "writer_lease_required",
    "persistent_writer_enablement",
    "unresolved_conflicts_deferred",
    "automatic_replay",
    "execution_state_ledger_relative_path",
})


class ProductionRealImportAuthorizationError(RuntimeError):
    """Fail-closed document error containing only a stable reason code."""

    def __init__(self, reason_code: str) -> None:
        reason = sanitize_reason_code(reason_code)
        super().__init__(reason)
        self.reason_code = reason


def _sha(value: Any, reason: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ProductionRealImportAuthorizationError(reason)
    return value


def _commit(value: Any, reason: str) -> str:
    if not isinstance(value, str) or _COMMIT_RE.fullmatch(value) is None:
        raise ProductionRealImportAuthorizationError(reason)
    return value


def _identity(value: Any, reason: str) -> str:
    if not isinstance(value, str) or _IDENTITY_RE.fullmatch(value) is None:
        raise ProductionRealImportAuthorizationError(reason)
    return value


def _timestamp(value: Any, reason: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ProductionRealImportAuthorizationError(reason)
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise ProductionRealImportAuthorizationError(reason) from None
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ProductionRealImportAuthorizationError(reason)
    return parsed


def canonical_document_digest(
    document: Mapping[str, Any],
    *,
    digest_field: str,
) -> str:
    payload = dict(document)
    payload.pop(digest_field, None)
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def load_json_document(path: str | Path, *, reason_prefix: str) -> dict[str, Any]:
    target = Path(path)
    try:
        if not target.is_file() or target.stat().st_size > MAX_DOCUMENT_BYTES:
            raise ProductionRealImportAuthorizationError(
                f"{reason_prefix}_invalid"
            )
        import json

        document = json.loads(target.read_bytes().decode("utf-8"))
    except ProductionRealImportAuthorizationError:
        raise
    except (OSError, UnicodeDecodeError, ValueError):
        raise ProductionRealImportAuthorizationError(
            f"{reason_prefix}_invalid"
        ) from None
    if not isinstance(document, dict):
        raise ProductionRealImportAuthorizationError(f"{reason_prefix}_invalid")
    return document


def execution_contract_digest(package: Mapping[str, Any]) -> str:
    contract = package.get("execution_contract")
    if not isinstance(contract, Mapping):
        raise ProductionRealImportAuthorizationError(
            "approval_execution_contract_invalid"
        )
    return hashlib.sha256(canonical_json_bytes(dict(contract))).hexdigest()


def _nonnegative_int(value: Any, reason: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProductionRealImportAuthorizationError(reason)
    return value


def validate_approval_package(
    package: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not isinstance(package, Mapping) or set(package) != _APPROVAL_FIELDS:
        raise ProductionRealImportAuthorizationError(
            "approval_package_fields_mismatch"
        )
    if package.get("package_schema_version") != APPROVAL_PACKAGE_SCHEMA_VERSION:
        raise ProductionRealImportAuthorizationError(
            "approval_package_schema_mismatch"
        )
    if package.get("status") != APPROVAL_STATUS:
        raise ProductionRealImportAuthorizationError(
            "approval_package_status_mismatch"
        )
    if (
        package.get("production_execution_protocol_version")
        != PRODUCTION_EXECUTION_PROTOCOL_VERSION
    ):
        raise ProductionRealImportAuthorizationError(
            "approval_execution_protocol_mismatch"
        )
    expected_digest = canonical_document_digest(
        package,
        digest_field="package_digest",
    )
    if _sha(
        package.get("package_digest"),
        "approval_package_digest_invalid",
    ) != expected_digest:
        raise ProductionRealImportAuthorizationError(
            "approval_package_digest_mismatch"
        )
    created_at = _timestamp(
        package.get("created_at"),
        "approval_created_at_invalid",
    )
    expires_at = _timestamp(
        package.get("approval_expires_at"),
        "approval_expiry_invalid",
    )
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if expires_at <= created_at:
        raise ProductionRealImportAuthorizationError("approval_expiry_invalid")
    if expires_at <= current:
        raise ProductionRealImportAuthorizationError("approval_package_expired")
    _commit(package.get("code_commit"), "approval_code_commit_invalid")
    _sha(
        package.get("source_manifest_digest"),
        "approval_manifest_digest_invalid",
    )
    _sha(
        package.get("exact_import_plan_digest"),
        "approval_plan_digest_invalid",
    )
    _identity(package.get("one_time_plan_id"), "approval_plan_id_invalid")

    baseline = package.get("production_main_baseline")
    if not isinstance(baseline, Mapping) or set(baseline) != {
        "size",
        "sha256",
        "schema_fingerprint",
        "counts",
    }:
        raise ProductionRealImportAuthorizationError(
            "approval_database_baseline_invalid"
        )
    _nonnegative_int(baseline.get("size"), "approval_database_size_invalid")
    _sha(baseline.get("sha256"), "approval_database_digest_invalid")
    _sha(
        baseline.get("schema_fingerprint"),
        "approval_schema_fingerprint_invalid",
    )
    counts = baseline.get("counts")
    if not isinstance(counts, Mapping) or set(counts) != {
        "conversations",
        "messages",
        "assets",
    }:
        raise ProductionRealImportAuthorizationError(
            "approval_database_counts_invalid"
        )
    for value in counts.values():
        _nonnegative_int(value, "approval_database_counts_invalid")

    asset_baseline = package.get("production_asset_baseline")
    try:
        validated_asset_baseline = validate_asset_baseline(asset_baseline)
    except AssetBaselineError:
        raise ProductionRealImportAuthorizationError(
            "approval_asset_baseline_invalid"
        ) from None
    if validated_asset_baseline["relative_root"] != (
        PRODUCTION_ASSET_ROOT_RELATIVE_PATH
    ):
        raise ProductionRealImportAuthorizationError(
            "approval_asset_baseline_invalid"
        )
    imports_baseline = package.get("imports_baseline")
    if not isinstance(imports_baseline, Mapping) or set(imports_baseline) != {
        "file_count",
        "total_size",
        "tree_sha256",
    }:
        raise ProductionRealImportAuthorizationError(
            "approval_imports_baseline_invalid"
        )
    _nonnegative_int(
        imports_baseline.get("file_count"),
        "approval_imports_baseline_invalid",
    )
    _nonnegative_int(
        imports_baseline.get("total_size"),
        "approval_imports_baseline_invalid",
    )
    _sha(
        imports_baseline.get("tree_sha256"),
        "approval_imports_baseline_invalid",
    )

    archive = package.get("candidate_archive")
    if not isinstance(archive, Mapping) or set(archive) != {
        "candidate_id",
        "location",
        "size",
        "sha256",
    }:
        raise ProductionRealImportAuthorizationError(
            "approval_archive_invalid"
        )
    _identity(archive.get("candidate_id"), "approval_archive_identity_invalid")
    _nonnegative_int(archive.get("size"), "approval_archive_size_invalid")
    _sha(archive.get("sha256"), "approval_archive_digest_invalid")
    if (
        not isinstance(archive.get("location"), str)
        or not archive["location"].startswith("<registered_archive_root>/")
    ):
        raise ProductionRealImportAuthorizationError(
            "approval_archive_location_invalid"
        )

    before_counts = package.get("before_counts")
    after_counts = package.get("after_counts")
    if (
        not isinstance(before_counts, Mapping)
        or dict(before_counts) != dict(counts)
        or not isinstance(after_counts, Mapping)
        or set(after_counts) != {
            "conversations",
            "messages",
            "assets",
            "ingestion_asset_entities",
        }
    ):
        raise ProductionRealImportAuthorizationError(
            "approval_expected_counts_invalid"
        )
    for value in after_counts.values():
        _nonnegative_int(value, "approval_expected_counts_invalid")
    new_message_count = _nonnegative_int(
        package.get("new_message_count"),
        "approval_new_message_count_invalid",
    )
    if (
        after_counts["conversations"] != before_counts["conversations"]
        or after_counts["messages"] != before_counts["messages"] + new_message_count
        or after_counts["assets"] != before_counts["assets"]
    ):
        raise ProductionRealImportAuthorizationError(
            "approval_expected_counts_invalid"
        )
    _nonnegative_int(
        package.get("exact_duplicate_count"),
        "approval_duplicate_count_invalid",
    )
    _nonnegative_int(
        package.get("old_time_new_id_count"),
        "approval_old_time_count_invalid",
    )
    _nonnegative_int(
        package.get("staged_byte_forecast"),
        "approval_staged_bytes_invalid",
    )
    asset_counts = package.get("asset_counts")
    if not isinstance(asset_counts, Mapping) or set(asset_counts) != {
        "unique_mapped_logical_assets",
        "unique_content_entities",
        "ambiguous",
        "missing",
        "unsupported_or_external",
    }:
        raise ProductionRealImportAuthorizationError(
            "approval_asset_counts_invalid"
        )
    for value in asset_counts.values():
        _nonnegative_int(value, "approval_asset_counts_invalid")
    if (
        after_counts["ingestion_asset_entities"]
        != asset_counts["unique_content_entities"]
        or package.get("asset_classification")
        not in {
            "FULL_ASSET_PRODUCTION_READY",
            "PARTIAL_ASSET_PRODUCTION_READY",
            "TEXT_IMPORT_ONLY_READY",
        }
    ):
        raise ProductionRealImportAuthorizationError(
            "approval_asset_counts_invalid"
        )
    conflicts = package.get("conflict_counts")
    if not isinstance(conflicts, Mapping) or any(
        not isinstance(key, str)
        or not key
        or isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        for key, value in conflicts.items()
    ):
        raise ProductionRealImportAuthorizationError(
            "approval_conflict_counts_invalid"
        )
    backup = package.get("backup")
    if not isinstance(backup, Mapping):
        raise ProductionRealImportAuthorizationError("approval_backup_invalid")
    for field in ("backup_manifest_digest", "backup_set_digest"):
        _sha(backup.get(field), f"approval_{field}_invalid")
    relative_backup = backup.get("backup_manifest_relative_path")
    if (
        not isinstance(relative_backup, str)
        or not relative_backup.startswith("backups/imports/")
        or "\\" in relative_backup
        or ".." in relative_backup
    ):
        raise ProductionRealImportAuthorizationError("approval_backup_invalid")

    contract = package.get("execution_contract")
    if not isinstance(contract, Mapping) or set(contract) != _EXECUTION_CONTRACT_FIELDS:
        raise ProductionRealImportAuthorizationError(
            "approval_execution_contract_invalid"
        )
    required_contract = {
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
    if dict(contract) != required_contract:
        raise ProductionRealImportAuthorizationError(
            "approval_execution_contract_mismatch"
        )
    supersedes = package.get("supersedes")
    expected_superseded_digests = {
        "5912e6fe6fbcc8d7faa4e6c9c9f5e5644534da7e935fe971631c5a7b19d94279",
        "21894e17597e840be59065feb7f0e2efb1563b336fc44d3c1e238e3f19e1224d",
        "98ea836460c066850e0297453faa28eb94cd8d3359a24c01a384558030666e92",
    }
    if (
        not isinstance(supersedes, list)
        or len(supersedes) != 3
        or {
            item.get("package_digest")
            for item in supersedes
            if isinstance(item, Mapping)
        }
        != expected_superseded_digests
        or any(
            not isinstance(item, Mapping)
            or set(item) != {"package_digest", "classification"}
            or item.get("classification") != "superseded"
            for item in supersedes
        )
    ):
        raise ProductionRealImportAuthorizationError(
            "approval_supersedes_invalid"
        )

    expected_false = (
        "production_execution_authorized",
        "human_authorization_record_generated",
        "conflict_auto_decision",
        "historical_backfill",
        "v8302",
        "production_writer_persistent_enablement",
    )
    if any(package.get(field) is not False for field in expected_false):
        raise ProductionRealImportAuthorizationError(
            "approval_scope_boundary_invalid"
        )
    if package.get("human_approval_required") is not True:
        raise ProductionRealImportAuthorizationError(
            "approval_human_authority_invalid"
        )
    if package.get("final_conclusion") != "READY_FOR_REAL_IMPORT_APPROVAL":
        raise ProductionRealImportAuthorizationError(
            "approval_conclusion_invalid"
        )
    return dict(package)


@dataclass(frozen=True)
class ProductionRealImportAuthorization:
    authorization_id: str
    authorization_scope: str
    authorization_basis: str
    authorization_bound_by: str
    standing_authorization_text_digest: str
    approval_package_digest: str
    bound_approval_package_digest: str
    exact_import_plan_digest: str
    bound_import_plan_digest: str
    one_time_plan_id: str
    source_database_sha256: str
    source_schema_fingerprint: str
    source_archive_sha256: str
    source_manifest_digest: str
    bound_source_manifest_digest: str
    execution_contract_digest: str
    code_commit: str
    bound_code_commit: str
    imports_tree_sha256: str
    asset_baseline_digest: str
    backup_manifest_digest: str
    backup_set_digest: str
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
    ) -> "ProductionRealImportAuthorization":
        if not isinstance(document, Mapping) or set(document) != _AUTHORIZATION_FIELDS:
            raise ProductionRealImportAuthorizationError(
                "authorization_fields_mismatch"
            )
        expected_digest = canonical_document_digest(
            document,
            digest_field="authorization_digest",
        )
        if _sha(
            document.get("authorization_digest"),
            "authorization_digest_invalid",
        ) != expected_digest:
            raise ProductionRealImportAuthorizationError(
                "authorization_digest_mismatch"
            )
        if document.get("authorization_schema_version") != AUTHORIZATION_SCHEMA_VERSION:
            raise ProductionRealImportAuthorizationError(
                "authorization_schema_mismatch"
            )
        if document.get("authorization_scope") != AUTHORIZATION_SCOPE:
            raise ProductionRealImportAuthorizationError(
                "authorization_scope_mismatch"
            )
        if (
            document.get("authorization_basis") != "user_standing_delegation"
            or document.get("authorization_bound_by") != "main_control_window"
        ):
            raise ProductionRealImportAuthorizationError(
                "authorization_basis_invalid"
            )
        for field in (
            "authorization_id",
            "one_time_execution_id",
            "one_time_plan_id",
        ):
            _identity(document.get(field), f"{field}_invalid")
        for field in (
            "approval_package_digest",
            "exact_import_plan_digest",
            "source_database_sha256",
            "source_schema_fingerprint",
            "source_archive_sha256",
            "source_manifest_digest",
            "bound_source_manifest_digest",
            "execution_contract_digest",
            "standing_authorization_text_digest",
            "user_authorization_text_digest",
            "imports_tree_sha256",
            "asset_baseline_digest",
            "backup_manifest_digest",
            "backup_set_digest",
            "bound_approval_package_digest",
            "bound_import_plan_digest",
        ):
            _sha(document.get(field), f"{field}_invalid")
        _commit(document.get("code_commit"), "authorization_code_commit_invalid")
        _commit(
            document.get("bound_code_commit"),
            "authorization_bound_code_commit_invalid",
        )
        authorized_at = _timestamp(
            document.get("authorized_at"),
            "authorization_time_invalid",
        )
        expires_at = _timestamp(
            document.get("expires_at"),
            "authorization_expiry_invalid",
        )
        if expires_at <= authorized_at:
            raise ProductionRealImportAuthorizationError(
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
        authorized_at = _timestamp(
            self.authorized_at,
            "authorization_time_invalid",
        )
        expires_at = _timestamp(
            self.expires_at,
            "authorization_expiry_invalid",
        )
        if authorized_at > current:
            raise ProductionRealImportAuthorizationError(
                "authorization_not_yet_valid"
            )
        if expires_at <= current:
            raise ProductionRealImportAuthorizationError(
                "authorization_expired"
            )


def validate_authorization_binding(
    authorization: ProductionRealImportAuthorization,
    package: Mapping[str, Any],
    *,
    current_code_commit: str,
    now: datetime | None = None,
) -> None:
    validated = validate_approval_package(package, now=now)
    authorization.validate_time(now=now)
    authorization_start = _timestamp(
        authorization.authorized_at,
        "authorization_time_invalid",
    )
    authorization_expiry = _timestamp(
        authorization.expires_at,
        "authorization_expiry_invalid",
    )
    package_created = _timestamp(
        validated["created_at"],
        "approval_created_at_invalid",
    )
    package_expiry = _timestamp(
        validated["approval_expires_at"],
        "approval_expiry_invalid",
    )
    if (
        authorization_start < package_created
        or authorization_expiry > package_expiry
    ):
        raise ProductionRealImportAuthorizationError(
            "authorization_interval_outside_approval"
        )
    baseline = validated["production_main_baseline"]
    archive = validated["candidate_archive"]
    bindings = (
        (
            authorization.approval_package_digest,
            validated["package_digest"],
            "authorization_package_digest_mismatch",
        ),
        (
            authorization.bound_approval_package_digest,
            validated["package_digest"],
            "authorization_bound_package_digest_mismatch",
        ),
        (
            authorization.exact_import_plan_digest,
            validated["exact_import_plan_digest"],
            "authorization_plan_digest_mismatch",
        ),
        (
            authorization.bound_import_plan_digest,
            validated["exact_import_plan_digest"],
            "authorization_bound_plan_digest_mismatch",
        ),
        (
            authorization.one_time_plan_id,
            validated["one_time_plan_id"],
            "authorization_plan_id_mismatch",
        ),
        (
            authorization.source_database_sha256,
            baseline["sha256"],
            "authorization_database_digest_mismatch",
        ),
        (
            authorization.source_schema_fingerprint,
            baseline["schema_fingerprint"],
            "authorization_schema_fingerprint_mismatch",
        ),
        (
            authorization.source_archive_sha256,
            archive["sha256"],
            "authorization_archive_digest_mismatch",
        ),
        (
            authorization.source_manifest_digest,
            validated["source_manifest_digest"],
            "authorization_manifest_digest_mismatch",
        ),
        (
            authorization.bound_source_manifest_digest,
            validated["source_manifest_digest"],
            "authorization_bound_manifest_digest_mismatch",
        ),
        (
            authorization.execution_contract_digest,
            execution_contract_digest(validated),
            "authorization_execution_contract_mismatch",
        ),
        (
            authorization.code_commit,
            current_code_commit,
            "authorization_code_commit_mismatch",
        ),
        (
            authorization.bound_code_commit,
            current_code_commit,
            "authorization_bound_code_commit_mismatch",
        ),
        (
            validated["code_commit"],
            current_code_commit,
            "approval_code_commit_mismatch",
        ),
        (
            authorization.imports_tree_sha256,
            validated["imports_baseline"]["tree_sha256"],
            "authorization_imports_digest_mismatch",
        ),
        (
            authorization.asset_baseline_digest,
            validated["production_asset_baseline"]["baseline_digest"],
            "authorization_asset_baseline_digest_mismatch",
        ),
        (
            authorization.backup_manifest_digest,
            validated["backup"]["backup_manifest_digest"],
            "authorization_backup_manifest_digest_mismatch",
        ),
        (
            authorization.backup_set_digest,
            validated["backup"]["backup_set_digest"],
            "authorization_backup_set_digest_mismatch",
        ),
        (
            authorization.user_authorization_text_digest,
            authorization.standing_authorization_text_digest,
            "authorization_standing_text_digest_mismatch",
        ),
        (
            authorization.standing_authorization_text_digest,
            STANDING_AUTHORIZATION_TEXT_DIGEST,
            "authorization_standing_text_digest_invalid",
        ),
    )
    for actual, expected, reason in bindings:
        if actual != expected:
            raise ProductionRealImportAuthorizationError(reason)
