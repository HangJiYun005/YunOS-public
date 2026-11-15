"""Fail-closed one-time production entry for the approved real ChatGPT import.

This module is deliberately narrow.  It accepts only the code-defined Main
database, the code-defined content-addressed asset root, one exact v2 approval
package, and a separate HumanAuthorizationRecord.  It cannot execute v8302,
historical backfill, a generic ImportPlan, or persistent writer enablement.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import subprocess
from typing import Any, Mapping

from memory.asset_baseline import (
    AssetBaselineError,
    build_asset_baseline,
    capture_asset_baseline,
    require_asset_baseline_match,
    validate_asset_baseline,
)
from memory.db_path_policy import is_reparse_point
from memory.ingestion_adapters.chatgpt import (
    ALL_REACHABLE_AUDIT,
    CONTENT_SNIFF_MIME_V1,
    LEGACY_IMPORT_TEXT_V1,
    ChatGPTAdapter,
)
from memory.ingestion_approval import import_plan_digest
from memory.ingestion_asset_staging import prepare_batch_paths
from memory.ingestion_core import canonical_json_bytes, normalized_json_digest
from memory.ingestion_migrations import INGESTION_TABLES, MIGRATION_DIGEST
from memory.production_real_import_authorization import (
    APPROVAL_PACKAGE_SCHEMA_VERSION,
    AUTHORIZATION_SCHEMA_VERSION,
    PRODUCTION_ASSET_ROOT_RELATIVE_PATH,
    PRODUCTION_DATABASE_RELATIVE_PATH,
    ProductionRealImportAuthorization,
    ProductionRealImportAuthorizationError,
    canonical_document_digest,
    execution_contract_digest,
    load_json_document,
    validate_approval_package,
    validate_authorization_binding,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_MAIN_DATABASE = (
    PROJECT_ROOT / PRODUCTION_DATABASE_RELATIVE_PATH
).absolute()
PRODUCTION_ASSET_ROOT = (
    PROJECT_ROOT / PRODUCTION_ASSET_ROOT_RELATIVE_PATH
).absolute()
APPROVAL_OUTPUT_ROOT = (
    PROJECT_ROOT / "output" / "v8.3-real-import-asset-baseline-fix"
).absolute()
APPROVAL_PACKAGE_PATH = (
    APPROVAL_OUTPUT_ROOT / "production_real_import_approval_package_v3.json"
)
IMPORT_PLAN_ARTIFACT_PATH = APPROVAL_OUTPUT_ROOT / "import_plan_draft.json"
SOURCE_MANIFEST_ARTIFACT_PATH = (
    APPROVAL_OUTPUT_ROOT / "source_manifest_draft.json"
)
EXECUTION_BASE = APPROVAL_OUTPUT_ROOT / "executions"
EXECUTION_STATE_LEDGER = APPROVAL_OUTPUT_ROOT / "execution_state.jsonl"

SERVICE_RECORD_SCHEMA_VERSION = "v8.3-real-import-service-quiescence-1"
EXECUTION_STATE_SCHEMA_VERSION = "v8.3-real-import-execution-state-1"
EXECUTION_JOURNAL_SCHEMA_VERSION = "v8.3-real-import-execution-journal-1"
EXECUTION_DATABASE_SCHEMA_VERSION = "v8.3-real-import-production-execution-1"
VERIFICATION_SCHEMA_VERSION = "v8.3-real-import-production-verification-1"
ASSET_RECEIPT_SCHEMA_VERSION = "v8.3-real-import-asset-receipt-1"
ZERO_DIGEST = hashlib.sha256(b"").hexdigest()

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
_IDENTITY_RE = re.compile(r"[a-z][a-z0-9_.:-]{7,191}\Z")
_BACKUP_NAMES = {
    "Main": "main.db",
    "Profile": "profile.db",
    "Semantic": "semantic.db",
    "Runtime": "runtime.db",
}


class ProductionRealImportExecutionError(RuntimeError):
    """Privacy-safe production error with recovery classification."""

    def __init__(self, reason_code: str, *, recovery_required: bool = False):
        reason = re.sub(r"[^a-z0-9_.-]+", "_", str(reason_code).lower()).strip("_")
        super().__init__(reason or "production_real_import_failed")
        self.reason_code = reason or "production_real_import_failed"
        self.recovery_required = bool(recovery_required)


@dataclass(frozen=True)
class GitExecutionState:
    commit: str
    tracked_worktree_clean: bool
    staging_empty: bool


@dataclass(frozen=True)
class ProductionRealImportRequest:
    approval_package: Path
    authorization_record: Path
    import_plan_artifact: Path
    source_manifest_artifact: Path
    archive: Path
    production_db: Path
    production_asset_root: Path
    expected_package_digest: str
    expected_plan_digest: str
    expected_archive_sha256: str
    expected_database_sha256: str
    execution_root: Path


@dataclass(frozen=True)
class ProductionRealImportMaterial:
    source_manifest: Any
    normalized_bundle: Any
    import_plan: Any
    stage_plan: Any
    exact_envelope: Mapping[str, Any]
    new_message_ids: frozenset[str]


@dataclass(frozen=True)
class ProductionRealImportOutcome:
    status: str
    execution_id: str
    database_after_sha256: str | None
    verification_digest: str | None
    recovery_required: bool


def _utc(value: datetime | None = None) -> str:
    current = (value or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return current.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _sha(value: Any, reason: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ProductionRealImportExecutionError(reason)
    return value


def _commit(value: Any, reason: str) -> str:
    if not isinstance(value, str) or _COMMIT_RE.fullmatch(value) is None:
        raise ProductionRealImportExecutionError(reason)
    return value


def _identity(value: Any, reason: str) -> str:
    if not isinstance(value, str) or _IDENTITY_RE.fullmatch(value) is None:
        raise ProductionRealImportExecutionError(reason)
    return value


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _json(value: Any) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _same_path(left: Path, right: Path) -> bool:
    try:
        return os.path.normcase(str(left.resolve(strict=False))) == os.path.normcase(
            str(right.resolve(strict=False))
        )
    except OSError:
        return False


def _reject_reparse_components(path: Path) -> None:
    current = path
    seen: list[Path] = []
    while True:
        if current.exists() or current.is_symlink():
            seen.append(current)
        if current.parent == current:
            break
        current = current.parent
    if any(is_reparse_point(item) for item in seen):
        raise ProductionRealImportExecutionError("execution_reparse_forbidden")


def validate_fixed_production_paths(
    database: Path,
    asset_root: Path,
) -> tuple[Path, Path]:
    if not _same_path(database, PRODUCTION_MAIN_DATABASE):
        raise ProductionRealImportExecutionError("production_database_path_mismatch")
    if not _same_path(asset_root, PRODUCTION_ASSET_ROOT):
        raise ProductionRealImportExecutionError("production_asset_root_mismatch")
    _reject_reparse_components(database)
    _reject_reparse_components(asset_root)
    if not database.is_file():
        raise ProductionRealImportExecutionError("production_database_unavailable")
    return database.resolve(strict=True), asset_root.resolve(strict=False)


def capture_git_execution_state() -> GitExecutionState:
    def run(*args: str) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=PROJECT_ROOT,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return completed.stdout.strip()

    try:
        return GitExecutionState(
            commit=run("rev-parse", "HEAD"),
            tracked_worktree_clean=not bool(
                run("status", "--porcelain=v1", "--untracked-files=no")
            ),
            staging_empty=not bool(run("diff", "--cached", "--name-only")),
        )
    except (OSError, subprocess.SubprocessError):
        raise ProductionRealImportExecutionError("git_state_unavailable") from None


def validate_git_execution_state(
    state: GitExecutionState,
    *,
    expected_commit: str,
) -> None:
    _commit(state.commit, "git_commit_invalid")
    if state.commit != expected_commit:
        raise ProductionRealImportExecutionError("git_commit_mismatch")
    if not state.tracked_worktree_clean:
        raise ProductionRealImportExecutionError("tracked_worktree_dirty")
    if not state.staging_empty:
        raise ProductionRealImportExecutionError("staging_not_empty")


def _validate_controlled_execution_root(
    root: Path,
    *,
    execution_id: str,
) -> Path:
    expected = EXECUTION_BASE / execution_id
    if not _same_path(root, expected):
        raise ProductionRealImportExecutionError("execution_root_mismatch")
    _reject_reparse_components(root)
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve(strict=True)


def _port_is_closed(port: int) -> bool:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
        probe.settimeout(0.25)
        return probe.connect_ex(("127.0.0.1", port)) != 0


def validate_service_quiescence_record(
    root: Path,
    *,
    authorization: ProductionRealImportAuthorization,
    now: datetime | None = None,
) -> dict[str, Any]:
    path = root / "services_quiesced.json"
    document = _load_document(path, "service_quiescence")
    expected_fields = {
        "record_schema_version",
        "one_time_execution_id",
        "code_commit",
        "source_database_sha256",
        "services_quiesced",
        "listener_count_8765",
        "active_writer_count",
        "tailscale_service_running",
        "observed_at",
        "expires_at",
        "record_digest",
    }
    if set(document) != expected_fields:
        raise ProductionRealImportExecutionError(
            "service_quiescence_fields_mismatch"
        )
    if document.get("record_digest") != canonical_document_digest(
        document,
        digest_field="record_digest",
    ):
        raise ProductionRealImportExecutionError(
            "service_quiescence_digest_mismatch"
        )
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    try:
        observed = datetime.fromisoformat(
            str(document["observed_at"])[:-1] + "+00:00"
        )
        expires = datetime.fromisoformat(
            str(document["expires_at"])[:-1] + "+00:00"
        )
    except (ValueError, TypeError):
        raise ProductionRealImportExecutionError(
            "service_quiescence_time_invalid"
        ) from None
    if (
        document.get("record_schema_version") != SERVICE_RECORD_SCHEMA_VERSION
        or document.get("one_time_execution_id")
        != authorization.one_time_execution_id
        or document.get("code_commit") != authorization.code_commit
        or document.get("source_database_sha256")
        != authorization.source_database_sha256
        or document.get("services_quiesced") is not True
        or document.get("listener_count_8765") != 0
        or document.get("active_writer_count") != 0
        or document.get("tailscale_service_running") is not True
        or not (observed <= current < expires)
        or expires - observed > timedelta(minutes=15)
        or not _port_is_closed(8765)
    ):
        raise ProductionRealImportExecutionError("services_not_quiesced")
    return document


def _load_document(path: Path, reason_prefix: str) -> dict[str, Any]:
    try:
        return load_json_document(path, reason_prefix=reason_prefix)
    except ProductionRealImportAuthorizationError as exc:
        raise ProductionRealImportExecutionError(exc.reason_code) from None


def _read_json_lines(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    try:
        if path.stat().st_size > 16 * 1024 * 1024:
            raise ProductionRealImportExecutionError("execution_state_invalid")
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line:
                raise ProductionRealImportExecutionError("execution_state_invalid")
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ProductionRealImportExecutionError("execution_state_invalid")
            records.append(value)
    except ProductionRealImportExecutionError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ProductionRealImportExecutionError("execution_state_invalid") from None
    return records


def _validate_not_replayed(execution_id: str, root: Path) -> str:
    _identity(execution_id, "one_time_execution_id_invalid")
    if (root / "recovery_required.json").exists():
        raise ProductionRealImportExecutionError(
            "recovery_required_exists",
            recovery_required=True,
        )
    previous = ZERO_DIGEST
    for record in _read_json_lines(EXECUTION_STATE_LEDGER):
        if set(record) != {
            "state_schema_version",
            "one_time_execution_id",
            "authorization_digest",
            "approval_package_digest",
            "state",
            "recorded_at",
            "previous_state_digest",
            "state_digest",
        }:
            raise ProductionRealImportExecutionError("execution_state_invalid")
        if (
            record.get("state_schema_version") != EXECUTION_STATE_SCHEMA_VERSION
            or record.get("state") not in {"consumed", "recovery_required"}
            or record.get("previous_state_digest") != previous
            or record.get("state_digest")
            != canonical_document_digest(record, digest_field="state_digest")
        ):
            raise ProductionRealImportExecutionError("execution_state_invalid")
        previous = record["state_digest"]
        if record.get("one_time_execution_id") == execution_id:
            raise ProductionRealImportExecutionError(
                "one_time_execution_replayed"
            )
    return previous


def _append_execution_state(
    *,
    authorization: ProductionRealImportAuthorization,
    state: str,
    previous_digest: str,
) -> str:
    if state not in {"consumed", "recovery_required"}:
        raise ProductionRealImportExecutionError("execution_state_invalid")
    record = {
        "state_schema_version": EXECUTION_STATE_SCHEMA_VERSION,
        "one_time_execution_id": authorization.one_time_execution_id,
        "authorization_digest": authorization.authorization_digest,
        "approval_package_digest": authorization.approval_package_digest,
        "state": state,
        "recorded_at": _utc(),
        "previous_state_digest": previous_digest,
    }
    record["state_digest"] = canonical_document_digest(
        record,
        digest_field="state_digest",
    )
    EXECUTION_STATE_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    data = canonical_json_bytes(record) + b"\n"
    descriptor = os.open(
        EXECUTION_STATE_LEDGER,
        os.O_APPEND | os.O_CREAT | os.O_WRONLY,
        0o600,
    )
    try:
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return record["state_digest"]


class _Journal:
    def __init__(
        self,
        path: Path,
        authorization: ProductionRealImportAuthorization,
    ) -> None:
        self.path = path
        self.authorization = authorization
        self.previous = ZERO_DIGEST
        self.sequence = 0
        if path.exists():
            raise ProductionRealImportExecutionError("execution_journal_exists")

    def append(self, event_type: str, payload: Mapping[str, Any]) -> str:
        self.sequence += 1
        record = {
            "journal_schema_version": EXECUTION_JOURNAL_SCHEMA_VERSION,
            "one_time_execution_id": self.authorization.one_time_execution_id,
            "sequence": self.sequence,
            "event_type": event_type,
            "payload_digest": _digest(dict(payload)),
            "occurred_at": _utc(),
            "previous_event_digest": self.previous,
        }
        record["event_digest"] = canonical_document_digest(
            record,
            digest_field="event_digest",
        )
        data = canonical_json_bytes(record) + b"\n"
        descriptor = os.open(
            self.path,
            os.O_APPEND | os.O_CREAT | os.O_WRONLY,
            0o600,
        )
        try:
            os.write(descriptor, data)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self.previous = record["event_digest"]
        return self.previous


def _validate_archive(path: Path, package: Mapping[str, Any]) -> Path:
    _reject_reparse_components(path)
    if not path.is_absolute() or not path.is_file() or is_reparse_point(path):
        raise ProductionRealImportExecutionError("source_archive_unavailable")
    archive = package["candidate_archive"]
    if (
        path.stat().st_size != archive["size"]
        or _hash_file(path) != archive["sha256"]
    ):
        raise ProductionRealImportExecutionError("source_archive_fingerprint_mismatch")
    return path.resolve(strict=True)


def _tree_gate(
    root: Path,
    *,
    project_root: Path | None = None,
) -> dict[str, Any]:
    try:
        return capture_asset_baseline(
            root,
            project_root=project_root or PROJECT_ROOT,
        )
    except AssetBaselineError as exc:
        raise ProductionRealImportExecutionError(exc.reason_code) from None


def _require_asset_baseline(
    approved: Mapping[str, Any],
    actual: Mapping[str, Any],
) -> None:
    try:
        require_asset_baseline_match(approved, actual)
    except AssetBaselineError as exc:
        raise ProductionRealImportExecutionError(exc.reason_code) from None


def _require_pretransaction_asset_baseline(
    *,
    approved: Mapping[str, Any],
    asset_root: Path,
    pending: Mapping[str, Path],
    project_root: Path | None = None,
) -> None:
    try:
        validated = validate_asset_baseline(approved)
        expected_inventory = list(validated["inventory"])
        for digest, path in sorted(pending.items()):
            _sha(digest, "asset_pending_digest_invalid")
            try:
                relative = path.relative_to(asset_root).as_posix()
            except ValueError:
                raise AssetBaselineError(
                    "asset_pending_outside_root"
                ) from None
            if not path.is_file() or _hash_file(path) != digest:
                raise AssetBaselineError("asset_pending_verification_failed")
            expected_inventory.append({
                "relative_path": relative,
                "size": path.stat().st_size,
                "sha256": digest,
                "entry_type": "file",
            })
        expected_inventory.sort(
            key=lambda item: (
                item["relative_path"].casefold(),
                item["relative_path"],
            )
        )
        expected = build_asset_baseline(
            root_state=(
                "populated"
                if expected_inventory
                else validated["root_state"]
            ),
            inventory=expected_inventory,
        )
        require_asset_baseline_match(
            expected,
            capture_asset_baseline(
                asset_root,
                project_root=project_root or PROJECT_ROOT,
            ),
        )
    except AssetBaselineError as exc:
        raise ProductionRealImportExecutionError(exc.reason_code) from None


def _validate_backup(
    package: Mapping[str, Any],
) -> dict[str, Any]:
    from tools.v83_real_import_readiness import _database_audit

    backup = package["backup"]
    relative = backup.get("backup_manifest_relative_path")
    if not isinstance(relative, str) or "\\" in relative or ".." in relative:
        raise ProductionRealImportExecutionError("backup_manifest_path_invalid")
    manifest_path = (PROJECT_ROOT / relative).resolve(strict=False)
    backup_root = (PROJECT_ROOT / "backups" / "imports").resolve()
    try:
        manifest_path.relative_to(backup_root)
    except ValueError:
        raise ProductionRealImportExecutionError("backup_manifest_path_invalid")
    _reject_reparse_components(manifest_path)
    manifest = _load_document(manifest_path, "backup_manifest")
    payload = dict(manifest)
    recorded = payload.pop("manifest_digest", None)
    if (
        recorded != backup.get("backup_manifest_digest")
        or _digest(payload) != recorded
        or _digest(manifest.get("database_results"))
        != backup.get("backup_set_digest")
        or manifest.get("restore_rehearsal", {}).get("passed") is not True
    ):
        raise ProductionRealImportExecutionError("backup_manifest_mismatch")
    for label, name in _BACKUP_NAMES.items():
        row = manifest.get("database_results", {}).get(label)
        if not isinstance(row, Mapping):
            raise ProductionRealImportExecutionError("backup_database_missing")
        path = manifest_path.parent / name
        audit = _database_audit(label, path)
        if (
            audit["file_gates"]["db"]["size"] != row.get("backup_size")
            or audit["file_gates"]["db"]["sha256"] != row.get("backup_sha256")
            or audit["schema_fingerprint"] != row.get("schema_fingerprint")
            or audit["logical_digest"] != row.get("logical_digest")
            or audit["integrity_check"] != ["ok"]
            or audit["foreign_key_violation_count"] != 0
            or not audit["immutable_read_gates_unchanged"]
        ):
            raise ProductionRealImportExecutionError("backup_database_mismatch")
    return {
        "manifest_path": manifest_path,
        "manifest": manifest,
    }


def _exact_envelope_digest(envelope: Mapping[str, Any]) -> str:
    payload = dict(envelope)
    payload.pop("exact_import_plan_digest", None)
    payload.pop("one_time_plan_id", None)
    return _digest(payload)


def _prepare_material(
    *,
    archive: Path,
    database: Path,
    package: Mapping[str, Any],
    import_plan_artifact: Mapping[str, Any],
    source_manifest_artifact: Mapping[str, Any],
) -> ProductionRealImportMaterial:
    from memory.ingestion_assets import AssetStageLimits, build_asset_stage_plan
    from tools.v83_real_import_readiness import (
        _asset_summary,
        _build_target_snapshot,
        _database_audit,
        _digest as readiness_digest,
    )

    if set(import_plan_artifact) != {
        "exact_import_plan",
        "action_counts",
        "conflict_counts",
        "asset_stage_plan_digest",
        "asset_stage_expected_delta",
        "asset_promotion_set_digest",
        "unresolved_asset_conflict_set_digest",
    }:
        raise ProductionRealImportExecutionError("import_plan_artifact_invalid")
    envelope = import_plan_artifact["exact_import_plan"]
    if (
        not isinstance(envelope, Mapping)
        or envelope.get("exact_import_plan_digest")
        != package["exact_import_plan_digest"]
        or _exact_envelope_digest(envelope)
        != package["exact_import_plan_digest"]
        or envelope.get("one_time_plan_id") != package["one_time_plan_id"]
    ):
        raise ProductionRealImportExecutionError("exact_import_plan_digest_mismatch")
    main_audit = _database_audit("Main", database)
    adapter = ChatGPTAdapter(
        archive,
        timestamp=envelope["created_at"],
        text_policy=LEGACY_IMPORT_TEXT_V1,
        asset_mime_policy=CONTENT_SNIFF_MIME_V1,
    )
    bundle = adapter.build_normalized_bundle(
        batch_mode="incremental",
        branch_policy=ALL_REACHABLE_AUDIT,
    )
    target, dedup, new_messages = _build_target_snapshot(
        bundle,
        database,
        main_audit,
    )
    result = adapter.produce_import_plan(
        target_snapshot=target,
        batch_mode="incremental",
        branch_policy=ALL_REACHABLE_AUDIT,
    )
    plan = result.import_plan
    asset_summary = _asset_summary(adapter, bundle)
    manifest_digest = normalized_json_digest(result.manifest.to_dict())
    insert_digest = readiness_digest(
        sorted(item.provenance.source_record_id for item in new_messages)
    )
    exact_duplicate_digest = readiness_digest(
        sorted(
            item.provenance.source_record_id
            for item in bundle.messages
            if item not in new_messages
        )
    )
    conflict_digest = readiness_digest([
        {
            "conflict_id": item.conflict_id,
            "conflict_type": item.conflict_type,
            "record_kind": item.record_kind,
            "source_identity": item.source_identity,
        }
        for item in plan.conflicts
    ])
    stage_plan = build_asset_stage_plan(
        import_plan=plan,
        attachments=bundle.attachments,
        asset_entities=bundle.asset_entities,
        occurrences=None,
        limits=AssetStageLimits(max_operation_seconds=900.0),
    )
    checks = {
        "core_import_plan_digest": import_plan_digest(plan),
        "production_main_sha256": main_audit["file_gates"]["db"]["sha256"],
        "production_main_size": main_audit["file_gates"]["db"]["size"],
        "production_schema_fingerprint": main_audit["schema_fingerprint"],
        "production_asset_baseline_digest": package[
            "production_asset_baseline"
        ]["baseline_digest"],
        "imports_tree_sha256": package["imports_baseline"]["tree_sha256"],
        "source_archive_sha256": _hash_file(archive),
        "source_archive_size": archive.stat().st_size,
        "source_manifest_digest": manifest_digest,
        "adapter_name": result.manifest.adapter_name,
        "adapter_version": result.manifest.adapter_version,
        "adapter_commit": package["code_commit"],
        "normalized_records_digest": bundle.normalized_digest,
        "dedup_result_digest": readiness_digest(dedup),
        "conflict_set_digest": conflict_digest,
        "asset_inventory_digest": readiness_digest(asset_summary),
        "insert_message_id_digest": insert_digest,
        "exact_duplicate_id_digest": exact_duplicate_digest,
        "asset_promotion_entity_count": len(bundle.asset_entities),
        "unresolved_conflict_count": len(plan.conflicts),
        "backup_manifest_digest": package["backup"]["backup_manifest_digest"],
        "execution_contract_digest": execution_contract_digest(package),
    }
    for key, actual in checks.items():
        if envelope.get(key) != actual:
            raise ProductionRealImportExecutionError(
                f"exact_plan_{key}_mismatch"
            )
    if (
        source_manifest_artifact.get("source_manifest_digest") != manifest_digest
        or source_manifest_artifact.get("source_manifest")
        != result.manifest.to_dict()
        or import_plan_artifact["asset_stage_plan_digest"]
        != stage_plan.stage_plan_digest
        or import_plan_artifact["asset_promotion_set_digest"]
        != readiness_digest(sorted(
            item.entity_digest for item in stage_plan.operations
        ))
    ):
        raise ProductionRealImportExecutionError("material_artifact_mismatch")
    if (
        len(new_messages) != package["new_message_count"]
        or len(plan.conflicts) != sum(package["conflict_counts"].values())
        or len(bundle.asset_entities)
        != package["asset_counts"]["unique_content_entities"]
    ):
        raise ProductionRealImportExecutionError("material_count_mismatch")
    return ProductionRealImportMaterial(
        source_manifest=result.manifest,
        normalized_bundle=bundle,
        import_plan=plan,
        stage_plan=stage_plan,
        exact_envelope=dict(envelope),
        new_message_ids=frozenset(
            item.provenance.source_record_id for item in new_messages
        ),
    )


def _stage_material(
    *,
    archive: Path,
    material: ProductionRealImportMaterial,
    execution_root: Path,
) -> tuple[Any, Any]:
    from memory.ingestion_asset_staging import (
        AssetStageLimits,
        ZipAssetSource,
        execute_asset_stage_plan,
        inspect_asset_stage_batch,
    )
    from tools.v83_real_import_readiness import _source_member_map

    adapter = ChatGPTAdapter(
        archive,
        timestamp=material.exact_envelope["created_at"],
        text_policy=LEGACY_IMPORT_TEXT_V1,
        asset_mime_policy=CONTENT_SNIFF_MIME_V1,
    )
    member_map = _source_member_map(adapter)
    sources = [
        ZipAssetSource(
            operation_id=operation.operation_id,
            archive_path=archive,
            member_name=member_map[operation.entity_digest],
        )
        for operation in material.stage_plan.operations
    ]
    limits = AssetStageLimits(max_operation_seconds=900.0)
    stage_root = execution_root / "stage"
    result = execute_asset_stage_plan(
        plan=material.stage_plan,
        sources=sources,
        stage_root=stage_root,
        limits=limits,
    )
    inspection = inspect_asset_stage_batch(
        stage_root=stage_root,
        batch_id=material.stage_plan.batch_id,
        plan=material.stage_plan,
    )
    if (
        result.status not in {"staged", "staged_with_warnings"}
        or inspection.status not in {"staged", "staged_with_warnings"}
        or inspection.verified_entities != len(material.stage_plan.operations)
    ):
        raise ProductionRealImportExecutionError("asset_stage_incomplete")
    return result, inspection


def _copy_pending_assets(
    *,
    material: ProductionRealImportMaterial,
    execution_root: Path,
    asset_root: Path,
    execution_id: str,
) -> dict[str, Path]:
    stage_paths = prepare_batch_paths(
        execution_root / "stage",
        material.stage_plan.batch_id,
    )
    pending_root = asset_root / "_pending" / execution_id
    pending: dict[str, Path] = {}
    for operation in material.stage_plan.operations:
        source = stage_paths.verified / ("sha256_" + operation.entity_digest)
        target = (
            pending_root
            / "sha256"
            / operation.entity_digest[:2]
            / operation.entity_digest
        )
        _reject_reparse_components(target.parent)
        target.parent.mkdir(parents=True, exist_ok=True)
        _reject_reparse_components(target.parent)
        shutil.copy2(source, target)
        if (
            target.stat().st_size != operation.actual_size
            or _hash_file(target) != operation.entity_digest
        ):
            raise ProductionRealImportExecutionError(
                "asset_pending_verification_failed"
            )
        pending[operation.entity_digest] = target
    return pending


def _remove_empty_parents(path: Path, stop: Path) -> None:
    current = path
    while current != stop and current.exists():
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def _rollback_asset_files(
    created: list[Path],
    pending: Mapping[str, Path],
    asset_root: Path,
) -> None:
    for path in reversed(created):
        if path.is_file():
            path.unlink()
        _remove_empty_parents(path.parent, asset_root)
    for path in pending.values():
        if path.is_file():
            path.unlink()
        _remove_empty_parents(path.parent, asset_root)


def _cleanup_owned_pending_root(asset_root: Path, execution_id: str) -> None:
    _identity(execution_id, "one_time_execution_id_invalid")
    pending_root = asset_root / "_pending" / execution_id
    if not pending_root.exists():
        return
    _reject_reparse_components(pending_root)
    for path in sorted(
        pending_root.rglob("*"),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        if path.is_symlink() or is_reparse_point(path):
            raise ProductionRealImportExecutionError(
                "asset_pending_reparse_forbidden"
            )
        if path.is_file():
            path.unlink()
        elif path.is_dir():
            path.rmdir()
    pending_root.rmdir()
    _remove_empty_parents(pending_root.parent, asset_root)


def _event_digest(
    *,
    identity: str,
    sequence: int,
    event_type: str,
    previous_state: str,
    next_state: str,
    occurred_at: str,
    reason_code: str,
    payload_digest: str,
    previous_event_digest: str,
    schema_version: str,
) -> tuple[str, str]:
    payload = {
        "identity": identity,
        "sequence": sequence,
        "event_type": event_type,
        "previous_state": previous_state,
        "next_state": next_state,
        "occurred_at": occurred_at,
        "actor_kind": "human_authorized_production_entry",
        "reason_code": reason_code,
        "payload_digest": payload_digest,
        "previous_event_digest": previous_event_digest,
        "event_schema_version": schema_version,
    }
    digest = _digest(payload)
    return "event_" + digest, digest


def _timestamp_epoch(value: str | None) -> float | None:
    if value is None:
        return None
    return datetime.fromisoformat(value[:-1] + "+00:00").timestamp()


def _legacy_preservation_digest(
    database: Path,
    *,
    excluded_message_ids: frozenset[str] = frozenset(),
) -> str:
    uri = database.resolve(strict=True).as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.execute("PRAGMA query_only=ON")
        payload: dict[str, Any] = {
            "conversations": [
                list(row)
                for row in connection.execute(
                    "SELECT * FROM conversations ORDER BY conversation_id"
                )
            ],
            "assets": [
                list(row)
                for row in connection.execute(
                    "SELECT * FROM assets ORDER BY asset_file"
                )
            ],
            "messages": [],
        }
        columns = [
            str(row[1])
            for row in connection.execute("PRAGMA table_info(messages)")
        ]
        query = "SELECT * FROM messages ORDER BY message_id"
        for row in connection.execute(query):
            record = dict(zip(columns, row))
            if str(record.get("message_id")) not in excluded_message_ids:
                payload["messages"].append(list(row))
    return _digest(payload)


def _checkpoint_wal(database: Path) -> None:
    with closing(sqlite3.connect(database)) as connection:
        row = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    if row is not None and int(row[0]) != 0:
        raise ProductionRealImportExecutionError(
            "wal_checkpoint_busy",
            recovery_required=True,
        )
    wal = Path(str(database) + "-wal")
    if wal.exists() and wal.stat().st_size != 0:
        raise ProductionRealImportExecutionError(
            "wal_checkpoint_incomplete",
            recovery_required=True,
        )


def _insert_database_transaction(
    *,
    database: Path,
    asset_root: Path,
    material: ProductionRealImportMaterial,
    package: Mapping[str, Any],
    authorization: ProductionRealImportAuthorization,
    pending: Mapping[str, Path],
    project_root: Path | None = None,
) -> list[Path]:
    bundle = material.normalized_bundle
    plan = material.import_plan
    timestamp = _utc()
    approval_id = "approval_production_" + package["package_digest"][:48]
    created_assets: list[Path] = []
    _require_pretransaction_asset_baseline(
        approved=package["production_asset_baseline"],
        asset_root=asset_root,
        pending=pending,
        project_root=project_root,
    )
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("BEGIN IMMEDIATE")
        try:
            if connection.execute(
                "SELECT COUNT(*) FROM ingestion_leases"
            ).fetchone()[0]:
                raise ProductionRealImportExecutionError(
                    "writer_lease_conflict"
                )
            connection.execute(
                "INSERT INTO ingestion_leases VALUES(?,?,?,?,?,?,?,?)",
                (
                    "production_main",
                    "real_chatgpt_import",
                    authorization.one_time_execution_id,
                    _digest({
                        "authorization_digest": authorization.authorization_digest,
                        "execution_id": authorization.one_time_execution_id,
                    }),
                    timestamp,
                    _utc(datetime.now(timezone.utc) + timedelta(minutes=15)),
                    "active",
                    "v8.3-e-writer-lease-1",
                ),
            )
            if connection.execute(
                "SELECT COUNT(*) FROM ingestion_batches"
            ).fetchone()[0]:
                raise ProductionRealImportExecutionError(
                    "production_ingestion_state_not_empty"
                )
            source_manifest = material.source_manifest
            connection.execute(
                """INSERT INTO ingestion_source_manifests(
                     manifest_id,source_type,source_version,adapter_name,
                     adapter_version,archive_sha256,archive_size,archive_kind,
                     manifest_schema_version,validation_status,
                     record_counts_json,asset_counts_json,warnings_json,
                     created_at,scanned_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    source_manifest.manifest_id,
                    source_manifest.source_type,
                    source_manifest.source_version,
                    source_manifest.adapter_name,
                    source_manifest.adapter_version,
                    source_manifest.archive_sha256,
                    source_manifest.archive_size,
                    source_manifest.archive_kind,
                    source_manifest.manifest_schema_version,
                    source_manifest.validation_status,
                    _json(dict(source_manifest.record_counts)),
                    _json(dict(source_manifest.asset_counts)),
                    _json(list(source_manifest.warnings)),
                    source_manifest.discovered_at,
                    source_manifest.scanned_at,
                ),
            )
            connection.execute(
                """INSERT INTO ingestion_batches(
                     batch_id,manifest_id,batch_mode,current_state,plan_digest,
                     approval_digest,target_snapshot_fingerprint,
                     asset_stage_fingerprint,recovery_status,warning_count,
                     error_count,conflict_count,batch_schema_version,
                     created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    bundle.batch.batch_id,
                    plan.manifest_id,
                    bundle.batch.batch_mode,
                    "committed_pending_verification",
                    package["exact_import_plan_digest"],
                    package["package_digest"],
                    plan.production_snapshot_digest,
                    material.stage_plan.stage_plan_digest,
                    "none",
                    len(bundle.audit_summary.warning_codes),
                    0,
                    len(plan.conflicts),
                    "v8.3-import-batch-1",
                    timestamp,
                    timestamp,
                ),
            )
            for conflict in plan.conflicts:
                connection.execute(
                    """INSERT INTO ingestion_conflicts(
                         conflict_id,batch_id,conflict_type,severity,
                         evidence_digest,proposed_actions_json,default_action,
                         requires_approval,conflict_state,
                         conflict_schema_version,created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        conflict.conflict_id,
                        plan.batch_id,
                        conflict.conflict_type,
                        conflict.risk_level,
                        _digest(list(conflict.evidence_hashes)),
                        _json(list(conflict.allowed_decisions)),
                        "manual_review",
                        1,
                        "pending",
                        conflict.conflict_schema_version,
                        timestamp,
                    ),
                )
            ordinal_by_conversation: dict[str, int] = {}
            messages = {
                item.provenance.source_record_id: item
                for item in bundle.messages
                if item.provenance.source_record_id in material.new_message_ids
            }
            if len(messages) != len(material.new_message_ids):
                raise ProductionRealImportExecutionError(
                    "append_message_material_missing"
                )
            for item in sorted(
                messages.values(),
                key=lambda value: (
                    value.source_conversation_id,
                    value.created_at or "",
                    value.provenance.source_record_id,
                ),
            ):
                conversation_id = item.source_conversation_id
                if conversation_id not in ordinal_by_conversation:
                    current = connection.execute(
                        "SELECT COALESCE(MAX(ordinal),-1) FROM messages "
                        "WHERE conversation_id=?",
                        (conversation_id,),
                    ).fetchone()[0]
                    ordinal_by_conversation[conversation_id] = int(current)
                ordinal_by_conversation[conversation_id] += 1
                title = connection.execute(
                    "SELECT title FROM conversations WHERE conversation_id=?",
                    (conversation_id,),
                ).fetchone()
                if title is None:
                    raise ProductionRealImportExecutionError(
                        "conversation_parent_missing"
                    )
                connection.execute(
                    """INSERT INTO messages(
                         message_id,node_id,conversation_id,conversation_title,
                         ordinal,parent_id,children_json,role,author_name,
                         create_time,create_time_iso,update_time,update_time_iso,
                         content_type,text,status,weight,metadata_json,
                         source_platform,memory_space
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        item.provenance.source_record_id,
                        item.source_node_id,
                        conversation_id,
                        str(title[0] or ""),
                        ordinal_by_conversation[conversation_id],
                        item.parent_source_message_id,
                        _json(list(item.child_source_message_ids)),
                        item.role,
                        item.author,
                        _timestamp_epoch(item.created_at),
                        item.created_at,
                        _timestamp_epoch(item.updated_at),
                        item.updated_at,
                        item.content_type,
                        item.text,
                        item.status,
                        item.weight,
                        _json(dict(item.normalized_metadata)),
                        "chatgpt",
                        "general",
                    ),
                )
            conflict_ids = {item.source_identity for item in plan.conflicts}
            for item in bundle.conversations:
                raw_id = item.provenance.source_record_id
                connection.execute(
                    """INSERT INTO ingestion_source_bindings(
                         source_type,source_record_kind,source_record_id,
                         target_record_kind,target_record_id,manifest_id,batch_id,
                         source_digest,binding_state,binding_schema_version,
                         created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "chatgpt",
                        "conversation",
                        raw_id,
                        "conversation",
                        raw_id,
                        plan.manifest_id,
                        plan.batch_id,
                        item.content_hash,
                        (
                            "pending_conflict"
                            if item.source_identity in conflict_ids
                            else "verified_existing"
                        ),
                        "v8.3-source-binding-1",
                        timestamp,
                    ),
                )
            for item in bundle.messages:
                raw_id = item.provenance.source_record_id
                connection.execute(
                    """INSERT INTO ingestion_source_bindings(
                         source_type,source_record_kind,source_record_id,
                         target_record_kind,target_record_id,manifest_id,batch_id,
                         source_digest,binding_state,binding_schema_version,
                         created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "chatgpt",
                        "message",
                        raw_id,
                        "message",
                        raw_id,
                        plan.manifest_id,
                        plan.batch_id,
                        item.content_hash,
                        (
                            "inserted"
                            if raw_id in material.new_message_ids
                            else "verified_existing"
                        ),
                        "v8.3-source-binding-1",
                        timestamp,
                    ),
                )
            for entity in bundle.asset_entities:
                storage_key = (
                    "sha256/"
                    + entity.content_sha256[:2]
                    + "/"
                    + entity.content_sha256
                )
                connection.execute(
                    """INSERT INTO ingestion_asset_entities(
                         entity_digest,detected_mime_type,actual_size,
                         entity_state,storage_key,verification_digest,
                         first_batch_id,entity_schema_version,created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        entity.content_sha256,
                        entity.detected_mime,
                        entity.actual_size,
                        "verified",
                        storage_key,
                        _digest(entity.plan_descriptor()),
                        plan.batch_id,
                        "v8.3-asset-entity-1",
                        timestamp,
                    ),
                )
            available = [
                item
                for item in bundle.attachments
                if item.entity_state == "entity_available"
            ]
            for index, item in enumerate(available):
                connection.execute(
                    """INSERT INTO ingestion_attachment_bindings(
                         logical_media_id,source_attachment_id_digest,message_id,
                         entity_digest,manifest_id,batch_id,occurrence_index,
                         attachment_state,binding_schema_version,created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        item.record_id,
                        _digest(item.provenance.source_record_id),
                        item.source_message_id,
                        item.content_sha256,
                        plan.manifest_id,
                        plan.batch_id,
                        index,
                        "verified",
                        "v8.3-attachment-binding-1",
                        timestamp,
                    ),
                )
            connection.execute(
                """INSERT INTO ingestion_approvals(
                     approval_id,batch_id,plan_digest,stage_plan_digest,
                     target_snapshot_fingerprint,approval_digest,
                     approval_scope,expires_at,actor_kind,
                     approval_schema_version,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    approval_id,
                    plan.batch_id,
                    package["exact_import_plan_digest"],
                    material.stage_plan.stage_plan_digest,
                    plan.production_snapshot_digest,
                    package["package_digest"],
                    "exact_plan",
                    authorization.expires_at,
                    "human",
                    APPROVAL_PACKAGE_SCHEMA_VERSION,
                    authorization.authorized_at,
                ),
            )
            connection.execute(
                """INSERT INTO ingestion_executions(
                     execution_id,batch_id,approval_digest,execution_state,
                     database_before_fingerprint,database_after_fingerprint,
                     asset_before_fingerprint,asset_after_fingerprint,
                     verification_digest,recovery_status,
                     execution_schema_version,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    authorization.one_time_execution_id,
                    plan.batch_id,
                    package["package_digest"],
                    "committed_pending_verification",
                    package["production_main_baseline"]["sha256"],
                    None,
                    package["production_asset_baseline"]["baseline_digest"],
                    None,
                    None,
                    "none",
                    EXECUTION_DATABASE_SCHEMA_VERSION,
                    timestamp,
                    timestamp,
                ),
            )
            event_id, event_digest = _event_digest(
                identity=authorization.one_time_execution_id,
                sequence=1,
                event_type="transaction_committed",
                previous_state="authorized",
                next_state="committed_pending_verification",
                occurred_at=timestamp,
                reason_code="exact_plan_committed",
                payload_digest=package["exact_import_plan_digest"],
                previous_event_digest=ZERO_DIGEST,
                schema_version="v8.3-execution-event-1",
            )
            connection.execute(
                """INSERT INTO ingestion_execution_events VALUES(
                     ?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    authorization.one_time_execution_id,
                    1,
                    event_id,
                    "transaction_committed",
                    "authorized",
                    "committed_pending_verification",
                    timestamp,
                    "human_authorized_production_entry",
                    "exact_plan_committed",
                    package["exact_import_plan_digest"],
                    ZERO_DIGEST,
                    event_digest,
                    "v8.3-execution-event-1",
                ),
            )
            for digest, source in sorted(pending.items()):
                target = asset_root / "sha256" / digest[:2] / digest
                _reject_reparse_components(target.parent)
                target.parent.mkdir(parents=True, exist_ok=True)
                _reject_reparse_components(target.parent)
                if target.exists():
                    if _hash_file(target) != digest:
                        raise ProductionRealImportExecutionError(
                            "asset_target_collision"
                        )
                    source.unlink()
                    continue
                os.replace(source, target)
                created_assets.append(target)
            connection.execute(
                "DELETE FROM ingestion_leases "
                "WHERE scope_type=? AND scope_id=? AND owner_id=?",
                (
                    "production_main",
                    "real_chatgpt_import",
                    authorization.one_time_execution_id,
                ),
            )
            if connection.execute(
                "SELECT COUNT(*) FROM ingestion_leases"
            ).fetchone()[0]:
                raise ProductionRealImportExecutionError(
                    "writer_lease_release_failed"
                )
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ProductionRealImportExecutionError(
                    "transaction_foreign_key_violation"
                )
            connection.commit()
        except BaseException:
            connection.rollback()
            _rollback_asset_files(created_assets, pending, asset_root)
            raise
    return created_assets


def _verify_and_finalize(
    *,
    database: Path,
    asset_root: Path,
    project_root: Path | None = None,
    material: ProductionRealImportMaterial,
    package: Mapping[str, Any],
    authorization: ProductionRealImportAuthorization,
    legacy_digest_before: str,
) -> dict[str, Any]:
    from tools.v83_real_import_readiness import _database_audit

    _checkpoint_wal(database)
    audit = _database_audit("Main", database)
    expected = package["after_counts"]
    table_counts = audit["table_counts"]
    expected_ingestion = {
        "ingestion_source_manifests": 1,
        "ingestion_batches": 1,
        "ingestion_conflicts": sum(package["conflict_counts"].values()),
        "ingestion_decisions": 0,
        "ingestion_source_bindings": (
            len(material.normalized_bundle.conversations)
            + len(material.normalized_bundle.messages)
        ),
        "ingestion_asset_entities": package["asset_counts"][
            "unique_content_entities"
        ],
        "ingestion_attachment_bindings": package["asset_counts"][
            "unique_mapped_logical_assets"
        ],
        "ingestion_approvals": 1,
        "ingestion_executions": 1,
        "ingestion_execution_events": 1,
        "ingestion_verifications": 0,
        "ingestion_batch_events": 0,
        "ingestion_leases": 0,
    }
    legacy_after = _legacy_preservation_digest(
        database,
        excluded_message_ids=material.new_message_ids,
    )
    assets = _tree_gate(asset_root, project_root=project_root)
    invariants = {
        "integrity_ok": audit["integrity_check"] == ["ok"],
        "foreign_keys_ok": audit["foreign_key_violation_count"] == 0,
        "schema_unchanged": audit["schema_fingerprint"]
        == package["production_main_baseline"]["schema_fingerprint"],
        "conversation_count": table_counts.get("conversations")
        == expected["conversations"],
        "message_count": table_counts.get("messages") == expected["messages"],
        "legacy_asset_count": table_counts.get("assets") == expected["assets"],
        "legacy_rows_preserved": legacy_after == legacy_digest_before,
        "ingestion_counts": all(
            table_counts.get(table) == value
            for table, value in expected_ingestion.items()
        ),
        "asset_file_count": assets["inventory_count"]
        == package["asset_counts"]["unique_content_entities"],
        "asset_total_size": assets["physical_bytes"]
        == package["staged_byte_forecast"],
        "wal_empty": (
            not Path(str(database) + "-wal").exists()
            or Path(str(database) + "-wal").stat().st_size == 0
        ),
    }
    if not all(invariants.values()):
        raise ProductionRealImportExecutionError(
            "postcommit_verification_failed",
            recovery_required=True,
        )
    timestamp = _utc()
    verification_payload = {
        "execution_id": authorization.one_time_execution_id,
        "database_after_sha256": audit["file_gates"]["db"]["sha256"],
        "database_after_logical_digest": audit["logical_digest"],
        "asset_after_fingerprint": assets["baseline_digest"],
        "actual_counts": {
            key: table_counts.get(key, 0)
            for key in (
                "conversations",
                "messages",
                "assets",
                *sorted(expected_ingestion),
            )
        },
        "invariant_results": invariants,
        "verified_at": timestamp,
        "verification_schema_version": VERIFICATION_SCHEMA_VERSION,
    }
    verification_digest = _digest(verification_payload)
    verification_id = "verification_" + verification_digest
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                """INSERT INTO ingestion_verifications(
                     verification_id,execution_id,passed,actual_deltas_json,
                     expected_deltas_json,invariant_results_json,
                     database_after_fingerprint,asset_after_fingerprint,
                     verification_digest,verification_schema_version,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    verification_id,
                    authorization.one_time_execution_id,
                    1,
                    _json({
                        "messages": package["new_message_count"],
                        "asset_entities": package["asset_counts"][
                            "unique_content_entities"
                        ],
                    }),
                    _json({
                        "messages": package["new_message_count"],
                        "asset_entities": package["asset_counts"][
                            "unique_content_entities"
                        ],
                    }),
                    _json(invariants),
                    audit["logical_digest"],
                    assets["baseline_digest"],
                    verification_digest,
                    VERIFICATION_SCHEMA_VERSION,
                    timestamp,
                ),
            )
            connection.execute(
                """UPDATE ingestion_executions SET
                     execution_state=?,database_after_fingerprint=?,
                     asset_after_fingerprint=?,verification_digest=?,
                     recovery_status=?,updated_at=? WHERE execution_id=?""",
                (
                    "completed",
                    audit["logical_digest"],
                    assets["baseline_digest"],
                    verification_digest,
                    "none",
                    timestamp,
                    authorization.one_time_execution_id,
                ),
            )
            connection.execute(
                """UPDATE ingestion_batches SET
                     current_state=?,updated_at=? WHERE batch_id=?""",
                ("completed", timestamp, material.import_plan.batch_id),
            )
            previous = connection.execute(
                """SELECT event_digest FROM ingestion_execution_events
                   WHERE execution_id=? AND sequence=1""",
                (authorization.one_time_execution_id,),
            ).fetchone()
            if previous is None:
                raise ProductionRealImportExecutionError(
                    "execution_event_chain_missing"
                )
            event_id, event_digest = _event_digest(
                identity=authorization.one_time_execution_id,
                sequence=2,
                event_type="verification_passed",
                previous_state="committed_pending_verification",
                next_state="completed",
                occurred_at=timestamp,
                reason_code="production_verification_passed",
                payload_digest=verification_digest,
                previous_event_digest=str(previous[0]),
                schema_version="v8.3-execution-event-1",
            )
            connection.execute(
                """INSERT INTO ingestion_execution_events VALUES(
                     ?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    authorization.one_time_execution_id,
                    2,
                    event_id,
                    "verification_passed",
                    "committed_pending_verification",
                    "completed",
                    timestamp,
                    "human_authorized_production_entry",
                    "production_verification_passed",
                    verification_digest,
                    str(previous[0]),
                    event_digest,
                    "v8.3-execution-event-1",
                ),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    _checkpoint_wal(database)
    final_audit = _database_audit("Main", database)
    if (
        final_audit["integrity_check"] != ["ok"]
        or final_audit["foreign_key_violation_count"] != 0
        or final_audit["table_counts"].get("ingestion_verifications") != 1
        or final_audit["table_counts"].get("ingestion_execution_events") != 2
    ):
        raise ProductionRealImportExecutionError(
            "final_verification_record_failed",
            recovery_required=True,
        )
    verification_payload["database_final_sha256"] = final_audit[
        "file_gates"
    ]["db"]["sha256"]
    verification_payload["verification_digest"] = verification_digest
    return verification_payload


def _make_exact_precommit_copy(
    database: Path,
    root: Path,
    expected_sha256: str,
) -> Path:
    target = root / "recovery" / "main.precommit.db"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(database, target)
    if _hash_file(target) != expected_sha256:
        raise ProductionRealImportExecutionError(
            "precommit_recovery_copy_mismatch"
        )
    return target


def _restore_exact_precommit_copy(
    *,
    database: Path,
    precommit_copy: Path,
    expected_sha256: str,
    created_assets: list[Path],
    asset_root: Path,
    root: Path,
    authorization: ProductionRealImportAuthorization,
    reason_code: str,
    asset_root_existed_before: bool = True,
) -> None:
    failed = root / "recovery" / "main.failed.db"
    if database.is_file():
        shutil.copy2(database, failed)
    temporary = database.parent / (
        "." + database.name + "." + authorization.one_time_execution_id + ".restore"
    )
    shutil.copy2(precommit_copy, temporary)
    if _hash_file(temporary) != expected_sha256:
        raise ProductionRealImportExecutionError(
            "recovery_source_mismatch",
            recovery_required=True,
        )
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(database) + suffix)
        if sidecar.exists():
            sidecar.unlink()
    os.replace(temporary, database)
    for path in reversed(created_assets):
        if path.is_file():
            path.unlink()
        _remove_empty_parents(path.parent, asset_root)
    _cleanup_owned_pending_root(
        asset_root,
        authorization.one_time_execution_id,
    )
    if (
        not asset_root_existed_before
        and asset_root.is_dir()
        and not any(asset_root.iterdir())
    ):
        asset_root.rmdir()
    marker = {
        "one_time_execution_id": authorization.one_time_execution_id,
        "authorization_digest": authorization.authorization_digest,
        "reason_code": reason_code,
        "database_restored_sha256": _hash_file(database),
        "asset_rollback_completed": True,
        "recorded_at": _utc(),
    }
    marker["marker_digest"] = canonical_document_digest(
        marker,
        digest_field="marker_digest",
    )
    (root / "recovery_required.json").write_bytes(canonical_json_bytes(marker))


def _revalidate_immediate_prewrite(
    *,
    package: Mapping[str, Any],
    authorization: ProductionRealImportAuthorization,
    database: Path,
    asset_root: Path,
    execution_root: Path,
) -> None:
    from tools.v83_real_import_readiness import (
        _database_audit,
        _tree_gate as readiness_tree_gate,
    )

    state = capture_git_execution_state()
    validate_git_execution_state(state, expected_commit=package["code_commit"])
    try:
        validate_authorization_binding(
            authorization,
            package,
            current_code_commit=state.commit,
        )
    except ProductionRealImportAuthorizationError as exc:
        raise ProductionRealImportExecutionError(exc.reason_code) from None
    validate_service_quiescence_record(
        execution_root,
        authorization=authorization,
    )
    audit = _database_audit("Main", database)
    wal = Path(str(database) + "-wal")
    _require_asset_baseline(
        package["production_asset_baseline"],
        _tree_gate(asset_root),
    )
    if (
        audit["file_gates"]["db"]["sha256"]
        != package["production_main_baseline"]["sha256"]
        or audit["file_gates"]["db"]["size"]
        != package["production_main_baseline"]["size"]
        or audit["schema_fingerprint"]
        != package["production_main_baseline"]["schema_fingerprint"]
        or audit["integrity_check"] != ["ok"]
        or audit["foreign_key_violation_count"] != 0
        or (wal.exists() and wal.stat().st_size != 0)
        or any(audit["table_counts"].get(table) != 0 for table in INGESTION_TABLES)
        or readiness_tree_gate(PROJECT_ROOT / "imports")
        != package["imports_baseline"]
    ):
        raise ProductionRealImportExecutionError(
            "immediate_prewrite_gate_mismatch"
        )


def execute_authorized_real_import(
    request: ProductionRealImportRequest,
) -> ProductionRealImportOutcome:
    for value, reason in (
        (request.expected_package_digest, "expected_package_digest_invalid"),
        (request.expected_plan_digest, "expected_plan_digest_invalid"),
        (request.expected_archive_sha256, "expected_archive_digest_invalid"),
        (request.expected_database_sha256, "expected_database_digest_invalid"),
    ):
        _sha(value, reason)
    package_path = Path(request.approval_package)
    if not _same_path(package_path, APPROVAL_PACKAGE_PATH):
        raise ProductionRealImportExecutionError("approval_package_path_mismatch")
    if not _same_path(request.import_plan_artifact, IMPORT_PLAN_ARTIFACT_PATH):
        raise ProductionRealImportExecutionError("import_plan_path_mismatch")
    if not _same_path(
        request.source_manifest_artifact,
        SOURCE_MANIFEST_ARTIFACT_PATH,
    ):
        raise ProductionRealImportExecutionError("source_manifest_path_mismatch")
    package = _load_document(package_path, "approval_package")
    try:
        validate_approval_package(package)
    except ProductionRealImportAuthorizationError as exc:
        raise ProductionRealImportExecutionError(exc.reason_code) from None
    if (
        package["package_digest"] != request.expected_package_digest
        or package["exact_import_plan_digest"] != request.expected_plan_digest
        or package["candidate_archive"]["sha256"]
        != request.expected_archive_sha256
        or package["production_main_baseline"]["sha256"]
        != request.expected_database_sha256
    ):
        raise ProductionRealImportExecutionError("explicit_binding_mismatch")
    git_state = capture_git_execution_state()
    validate_git_execution_state(
        git_state,
        expected_commit=package["code_commit"],
    )
    database, asset_root = validate_fixed_production_paths(
        Path(request.production_db),
        Path(request.production_asset_root),
    )
    root_parent = Path(request.execution_root)
    authorization_path = Path(request.authorization_record)
    authorization_document = _load_document(
        authorization_path,
        "authorization_record",
    )
    try:
        authorization = ProductionRealImportAuthorization.from_mapping(
            authorization_document
        )
        validate_authorization_binding(
            authorization,
            package,
            current_code_commit=git_state.commit,
        )
    except ProductionRealImportAuthorizationError as exc:
        raise ProductionRealImportExecutionError(exc.reason_code) from None
    root = _validate_controlled_execution_root(
        root_parent,
        execution_id=authorization.one_time_execution_id,
    )
    expected_authorization_path = root / "human_authorization_record.json"
    if not _same_path(authorization_path, expected_authorization_path):
        raise ProductionRealImportExecutionError("authorization_record_path_mismatch")
    previous_state_digest = _validate_not_replayed(
        authorization.one_time_execution_id,
        root,
    )
    journal = _Journal(root / "execution_journal.jsonl", authorization)
    journal.append("authorization_validated", {
        "authorization_digest": authorization.authorization_digest,
        "package_digest": package["package_digest"],
    })
    validate_service_quiescence_record(
        root,
        authorization=authorization,
    )
    journal.append("service_quiescence_validated", {"port_8765_closed": True})
    archive = _validate_archive(Path(request.archive), package)
    _require_asset_baseline(
        package["production_asset_baseline"],
        _tree_gate(asset_root),
    )
    from tools.v83_real_import_readiness import (
        _database_audit,
        _tree_gate as readiness_tree_gate,
    )

    before = _database_audit("Main", database)
    if (
        before["file_gates"]["db"]["sha256"]
        != package["production_main_baseline"]["sha256"]
        or before["file_gates"]["db"]["size"]
        != package["production_main_baseline"]["size"]
        or before["schema_fingerprint"]
        != package["production_main_baseline"]["schema_fingerprint"]
        or before["integrity_check"] != ["ok"]
        or before["foreign_key_violation_count"] != 0
        or (
            Path(str(database) + "-wal").exists()
            and Path(str(database) + "-wal").stat().st_size != 0
        )
        or any(before["table_counts"].get(table) != 0 for table in INGESTION_TABLES)
        or before.get("migration_digest") != MIGRATION_DIGEST
        or readiness_tree_gate(PROJECT_ROOT / "imports")
        != package["imports_baseline"]
    ):
        raise ProductionRealImportExecutionError("production_baseline_mismatch")
    backup = _validate_backup(package)
    journal.append("backup_validated", {
        "backup_manifest_digest": package["backup"]["backup_manifest_digest"]
    })
    plan_artifact = _load_document(
        Path(request.import_plan_artifact),
        "import_plan_artifact",
    )
    manifest_artifact = _load_document(
        Path(request.source_manifest_artifact),
        "source_manifest_artifact",
    )
    material = _prepare_material(
        archive=archive,
        database=database,
        package=package,
        import_plan_artifact=plan_artifact,
        source_manifest_artifact=manifest_artifact,
    )
    journal.append("exact_material_validated", {
        "exact_import_plan_digest": package["exact_import_plan_digest"],
        "source_manifest_digest": package["source_manifest_digest"],
    })
    _stage_material(
        archive=archive,
        material=material,
        execution_root=root,
    )
    journal.append("asset_stage_validated", {
        "stage_plan_digest": material.stage_plan.stage_plan_digest,
        "entity_count": len(material.stage_plan.operations),
    })
    _revalidate_immediate_prewrite(
        package=package,
        authorization=authorization,
        database=database,
        asset_root=asset_root,
        execution_root=root,
    )
    journal.append("immediate_prewrite_gates_validated", {
        "database_sha256": package["production_main_baseline"]["sha256"],
        "asset_baseline_digest": package[
            "production_asset_baseline"
        ]["baseline_digest"],
    })
    precommit_copy = _make_exact_precommit_copy(
        database,
        root,
        package["production_main_baseline"]["sha256"],
    )
    legacy_digest = _legacy_preservation_digest(database)
    try:
        pending = _copy_pending_assets(
            material=material,
            execution_root=root,
            asset_root=asset_root,
            execution_id=authorization.one_time_execution_id,
        )
    except BaseException:
        _cleanup_owned_pending_root(
            asset_root,
            authorization.one_time_execution_id,
        )
        raise
    created_assets: list[Path] = []
    committed = False
    state_chain_tail = previous_state_digest
    try:
        created_assets = _insert_database_transaction(
            database=database,
            asset_root=asset_root,
            material=material,
            package=package,
            authorization=authorization,
            pending=pending,
        )
        _cleanup_owned_pending_root(
            asset_root,
            authorization.one_time_execution_id,
        )
        committed = True
        journal.append("transaction_committed", {
            "created_asset_count": len(created_assets),
            "new_message_count": package["new_message_count"],
        })
        verification = _verify_and_finalize(
            database=database,
            asset_root=asset_root,
            material=material,
            package=package,
            authorization=authorization,
            legacy_digest_before=legacy_digest,
        )
        journal.append("verification_passed", {
            "verification_digest": verification["verification_digest"],
            "database_final_sha256": verification["database_final_sha256"],
        })
        receipt = {
            "receipt_schema_version": ASSET_RECEIPT_SCHEMA_VERSION,
            "one_time_execution_id": authorization.one_time_execution_id,
            "package_digest": package["package_digest"],
            "asset_baseline": _tree_gate(asset_root),
            "created_asset_count": len(created_assets),
            "recorded_at": _utc(),
        }
        receipt["receipt_digest"] = canonical_document_digest(
            receipt,
            digest_field="receipt_digest",
        )
        (root / "asset_receipt.json").write_bytes(canonical_json_bytes(receipt))
        state_chain_tail = _append_execution_state(
            authorization=authorization,
            state="consumed",
            previous_digest=state_chain_tail,
        )
        journal.append("execution_completed", {
            "state": "consumed",
            "receipt_digest": receipt["receipt_digest"],
        })
        return ProductionRealImportOutcome(
            status="completed",
            execution_id=authorization.one_time_execution_id,
            database_after_sha256=verification["database_final_sha256"],
            verification_digest=verification["verification_digest"],
            recovery_required=False,
        )
    except BaseException as exc:
        reason = (
            exc.reason_code
            if isinstance(exc, ProductionRealImportExecutionError)
            else "unexpected_execution_failure"
        )
        if committed:
            _restore_exact_precommit_copy(
                database=database,
                precommit_copy=precommit_copy,
                expected_sha256=package["production_main_baseline"]["sha256"],
                created_assets=created_assets,
                asset_root=asset_root,
                root=root,
                authorization=authorization,
                reason_code=reason,
                asset_root_existed_before=package[
                    "production_asset_baseline"
                ]["root_state"] != "absent",
            )
            _append_execution_state(
                authorization=authorization,
                state="recovery_required",
                previous_digest=state_chain_tail,
            )
            journal.append("recovery_required", {"reason_code": reason})
            raise ProductionRealImportExecutionError(
                reason,
                recovery_required=True,
            ) from None
        journal.append("execution_failed_precommit", {"reason_code": reason})
        if isinstance(exc, ProductionRealImportExecutionError):
            raise
        raise ProductionRealImportExecutionError(reason) from None
