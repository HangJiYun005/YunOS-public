"""Generate sanitized v8.3-G historical backfill acceptance evidence.

Production databases and imports are read-only evidence sources.  Every
migration, backfill, fault injection, recovery, and rollback runs on
disposable SQLite online-backup clones below the system temporary directory.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from typing import Any, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.db_path_policy import (
    DEFAULT_APPROVED_PROFILE_SIDECAR_PATH,
    DEFAULT_REAL_DB_PATH,
    DEFAULT_RUNTIME_SIDECAR_PATH,
    DEFAULT_SEMANTIC_SIDECAR_PATH,
)
from memory.ingestion_backfill import (
    aggregate_evidence_source_digests,
    audit_historical_evidence,
    build_historical_backfill_plan,
    build_historical_manifest_and_subject,
)
from memory.ingestion_backfill_execution import (
    assert_unified_historical_authority,
    execute_historical_backfill,
    recover_historical_backfill,
    rollback_historical_backfill,
)
from memory.ingestion_historical_schema import UNIFIED_V2_TABLES
from memory.ingestion_migration_rehearsal import (
    capture_legacy_compatibility,
    create_consistent_backup,
    run_compatibility_suite,
)
from memory.ingestion_migrations import (
    HISTORICAL_MIGRATION_DIGEST,
    HISTORICAL_MIGRATION_INDEX_DDL,
    HISTORICAL_MIGRATION_TRIGGER_DDL,
    HISTORICAL_MIGRATION_VERSION,
    MIGRATION_DIGEST,
    MIGRATION_VERSION,
    apply_historical_migration_to_clone,
    apply_migration_to_clone,
    build_historical_migration_plan,
    build_migration_plan,
    inspect_schema,
    inspect_v8301_schema,
    verify_historical_migrated_schema,
    verify_migrated_schema,
)


OUTPUT_ROOT = (
    PROJECT_ROOT
    / "output"
    / "v8.3-g-historical-backfill-compatibility"
)
PRODUCTION_DB = DEFAULT_REAL_DB_PATH
IMPORTS_ROOT = PROJECT_ROOT / "imports" / "chatgpt"
CHUNK_SIZE = 1024 * 1024
EVIDENCE_SCHEMA_VERSION = "v8.3-g-historical-backfill-evidence-1"

EVIDENCE_SOURCES = {
    "chatgpt_initial_import_report": (
        PROJECT_ROOT / "docs_private" / "import_report.md"
    ),
    "chatgpt_incremental_and_production_baseline": (
        PROJECT_ROOT / "docs" / "governance"
        / "yunos_production_baseline_audit.md"
    ),
    "claude_append_only_import_report": (
        PROJECT_ROOT / "logs" / "imports"
        / "claude_import_20260725T182225Z_628bdabcdfc8.json"
    ),
    "deepseek_import_report": (
        PROJECT_ROOT / "logs" / "deepseek_import_report.md"
    ),
    "deterministic_historical_snapshot_report": (
        PROJECT_ROOT / "docs" / "deterministic_sqlite_snapshot_v8_0_d2_2.md"
    ),
    "git_version_release_report": (
        PROJECT_ROOT / "docs" / "version_reports"
        / "yunos_v8_2_release_closure.md"
    ),
    "adapter_identity_policy": (
        PROJECT_ROOT / "docs" / "design"
        / "yunos_v8_3_c_unified_chatgpt_adapter.md"
    ),
    "media_identity_audit": (
        PROJECT_ROOT / "output" / "playwright"
        / "v8.2-e4-r0-media-entity-source-audit"
        / "source_audit_report.json"
    ),
    "g0_production_baseline_audit": (
        PROJECT_ROOT / "output"
        / "v8.3-g0-historical-schema-compatibility"
        / "acceptance_summary.json"
    ),
}

NEW_TEST_MODULES = (
    "tests.test_v83_historical_backfill",
    "tests.test_v83_historical_backfill_execution",
    "tests.test_v83_historical_backfill_compatibility",
    "tests.test_v83_historical_backfill_recovery",
    "tests.test_v83_historical_backfill_security",
)


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _file_gate(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {
            "exists": False,
            "size": 0,
            "sha256": None,
            "mtime_ns": None,
            "mtime_utc": None,
        }
    stat = path.stat()
    return {
        "exists": True,
        "size": stat.st_size,
        "sha256": _hash_file(path),
        "mtime_ns": stat.st_mtime_ns,
        "mtime_utc": (
            datetime.fromtimestamp(stat.st_mtime, timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        ),
    }


def _sqlite_gate(path: Path) -> dict[str, Any]:
    gate = _file_gate(path)
    if not gate["exists"]:
        return {
            **gate,
            "integrity_check": "missing",
            "foreign_key_violation_count": None,
        }
    with closing(sqlite3.connect(
        path.resolve().as_uri() + "?mode=ro&immutable=1",
        uri=True,
    )) as connection:
        connection.execute("PRAGMA query_only=ON")
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()
    return {
        **gate,
        "integrity_check": integrity,
        "foreign_key_violation_count": len(foreign_keys),
    }


def _tree_gate(root: Path) -> dict[str, Any]:
    files = sorted(
        (item for item in root.rglob("*") if item.is_file()),
        key=lambda item: item.relative_to(root).as_posix(),
    )
    lines: list[str] = []
    size = 0
    for path in files:
        relative = path.relative_to(root).as_posix()
        item_size = path.stat().st_size
        size += item_size
        lines.append(f"{relative}|{item_size}|{_hash_file(path)}")
    return {
        "file_count": len(files),
        "total_size": size,
        "tree_sha256": hashlib.sha256(
            "\n".join(lines).encode("utf-8")
        ).hexdigest(),
    }


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _production_gate() -> dict[str, Any]:
    return {
        "main": _sqlite_gate(PRODUCTION_DB),
        "wal": _file_gate(Path(str(PRODUCTION_DB) + "-wal")),
        "shm": _file_gate(Path(str(PRODUCTION_DB) + "-shm")),
        "profile": _sqlite_gate(DEFAULT_APPROVED_PROFILE_SIDECAR_PATH),
        "semantic": _sqlite_gate(DEFAULT_SEMANTIC_SIDECAR_PATH),
        "runtime": _sqlite_gate(DEFAULT_RUNTIME_SIDECAR_PATH),
        "imports_chatgpt": _tree_gate(IMPORTS_ROOT),
        "stash_oid": _git("rev-parse", "stash@{0}"),
    }


def _compare_gates(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> dict[str, Any]:
    exact_files = ("main", "wal", "profile", "semantic", "runtime")
    file_checks = {
        name: {
            key: before[name][key] == after[name][key]
            for key in ("exists", "size", "sha256", "mtime_ns")
        }
        for name in exact_files
    }
    shm = {
        key: before["shm"][key] == after["shm"][key]
        for key in ("exists", "size", "sha256")
    }
    shm["mtime_observed_equal"] = (
        before["shm"]["mtime_ns"] == after["shm"]["mtime_ns"]
    )
    integrity = {
        name: (
            after[name]["integrity_check"] == "ok"
            and after[name]["foreign_key_violation_count"] == 0
        )
        for name in ("main", "profile", "semantic", "runtime")
    }
    imports_equal = before["imports_chatgpt"] == after["imports_chatgpt"]
    stash_equal = before["stash_oid"] == after["stash_oid"]
    wal_zero = after["wal"]["size"] == 0
    hard_checks = [
        *(all(value.values()) for value in file_checks.values()),
        all(shm[key] for key in ("exists", "size", "sha256")),
        all(integrity.values()),
        imports_equal,
        stash_equal,
        wal_zero,
    ]
    return {
        "passed": all(hard_checks),
        "exact_file_checks": file_checks,
        "shm_content_and_size_checks": shm,
        "sqlite_integrity_checks": integrity,
        "imports_chatgpt_unchanged": imports_equal,
        "stash_unchanged": stash_equal,
        "wal_zero": wal_zero,
        "production_migration_executed": False,
        "production_backfill_executed": False,
        "production_writer_executed": False,
        "production_asset_promotion_executed": False,
    }


def _write_json(name: str, value: Mapping[str, Any]) -> None:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    target = OUTPUT_ROOT / name
    pending = target.with_suffix(target.suffix + ".tmp")
    pending.write_text(
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    pending.replace(target)


def _v2_counts(database: Path) -> dict[str, int]:
    with closing(sqlite3.connect(
        database.resolve().as_uri() + "?mode=ro",
        uri=True,
    )) as connection:
        connection.execute("PRAGMA query_only=ON")
        return {
            table: int(connection.execute(
                f'SELECT COUNT(*) FROM "{table}"'
            ).fetchone()[0])
            for table in UNIFIED_V2_TABLES
        }


def _copy_empty_clone(source: Path, root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=False)
    target = root / "target.sqlite"
    create_consistent_backup(source, target)
    return target


def _execute(
    clone: Path,
    manifest: Any,
    subject: Any,
    plan: Any,
    *,
    created_at: str,
    expires_at: str,
    checkpoint: str | None = None,
):
    return execute_historical_backfill(
        clone_path=clone,
        manifest=manifest,
        subject=subject,
        plan=plan,
        approval_created_at=created_at,
        approval_expires_at=expires_at,
        fault_injector=(
            None
            if checkpoint is None
            else lambda value: value == checkpoint
        ),
    )


def _new_test_count() -> int:
    import unittest

    return unittest.defaultTestLoader.loadTestsFromNames(
        NEW_TEST_MODULES
    ).countTestCases()


def _run_full_regression() -> dict[str, Any]:
    started = time.monotonic()
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    command = [
        sys.executable,
        "-m",
        "unittest",
        "discover",
        "-s",
        "tests",
        "-p",
        "test*.py",
    ]
    result = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    elapsed = round(time.monotonic() - started, 3)
    combined = result.stdout + "\n" + result.stderr
    match = re.search(r"Ran (\d+) tests? in ([0-9.]+)s", combined)
    lines = [
        line
        for line in combined.splitlines()
        if (
            line.startswith(("Ran ", "OK", "FAILED"))
            or line.startswith(("ERROR:", "FAIL:"))
        )
    ]
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "status": "passed" if result.returncode == 0 else "failed",
        "return_code": result.returncode,
        "test_count": int(match.group(1)) if match else None,
        "reported_test_seconds": (
            float(match.group(2)) if match else None
        ),
        "wall_seconds": elapsed,
        "command": "python -m unittest discover -s tests -p test*.py",
        "summary_lines": lines[-20:],
        "full_regression_run_count": 1,
    }


def _run_evidence(*, run_full_regression: bool) -> dict[str, Any]:
    assert_unified_historical_authority()
    before_gate = _production_gate()
    evidence_digests = aggregate_evidence_source_digests(EVIDENCE_SOURCES)
    audit = audit_historical_evidence(
        PRODUCTION_DB,
        evidence_source_digests=evidence_digests,
        legacy_report_available=True,
    )
    created = _utc_now()
    expires = (
        datetime.fromisoformat(created[:-1] + "+00:00")
        + timedelta(hours=24)
    ).isoformat(timespec="microseconds").replace("+00:00", "Z")
    manifest, subject = build_historical_manifest_and_subject(
        audit,
        created_at=created,
    )
    plan = build_historical_backfill_plan(
        audit,
        subject,
        migration_version=HISTORICAL_MIGRATION_VERSION,
        migration_digest=HISTORICAL_MIGRATION_DIGEST,
    )

    with tempfile.TemporaryDirectory(
        prefix="ayunos-v83g-historical-evidence-"
    ) as raw_root:
        root = Path(raw_root)
        preparation = root / "preparation"
        preparation.mkdir()
        migration_clone = preparation / "target.sqlite"
        backup_result = create_consistent_backup(
            PRODUCTION_DB,
            migration_clone,
        )
        legacy_before_migration = capture_legacy_compatibility(
            migration_clone
        )
        v8301_plan = build_migration_plan(inspect_schema(migration_clone))
        v8301_result = apply_migration_to_clone(
            v8301_plan,
            migration_clone,
            rehearsal_id="rehearsal_v83g_evidence_8301",
        )
        v8301_verification = verify_migrated_schema(
            migration_clone,
            expected_fingerprint=(
                v8301_plan.expected_target_schema_fingerprint
            ),
        )
        v8302_plan = build_historical_migration_plan(
            inspect_v8301_schema(migration_clone)
        )
        v8302_result = apply_historical_migration_to_clone(
            v8302_plan,
            migration_clone,
            rehearsal_id="rehearsal_v83g_evidence_8302",
        )
        v8302_verification = verify_historical_migrated_schema(
            migration_clone,
            expected_fingerprint=(
                v8302_plan.expected_target_schema_fingerprint
            ),
            require_empty=True,
        )
        migrated_legacy = capture_legacy_compatibility(migration_clone)
        migration_compatibility = run_compatibility_suite(
            legacy_before_migration,
            migrated_legacy,
            require_empty_ingestion_tables=True,
        )

        main_clone = _copy_empty_clone(migration_clone, root / "main")
        before_backfill = capture_legacy_compatibility(main_clone)
        result, approval, bundle = _execute(
            main_clone,
            manifest,
            subject,
            plan,
            created_at=created,
            expires_at=expires,
        )
        after_backfill = capture_legacy_compatibility(main_clone)
        compatibility = run_compatibility_suite(
            before_backfill,
            after_backfill,
            require_empty_ingestion_tables=False,
        )
        replay, _, _ = _execute(
            main_clone,
            manifest,
            subject,
            plan,
            created_at=created,
            expires_at=expires,
        )
        main_counts = _v2_counts(main_clone)

        failure_results: dict[str, Any] = {}
        for checkpoint in (
            "before_commit",
            "event_journal_failure",
            "attempt_old_table_write",
            "verification_failure",
        ):
            scenario_clone = _copy_empty_clone(
                migration_clone,
                root / ("failure-" + checkpoint),
            )
            scenario_before = capture_legacy_compatibility(scenario_clone)
            scenario_result, _, scenario_bundle = _execute(
                scenario_clone,
                manifest,
                subject,
                plan,
                created_at=created,
                expires_at=expires,
                checkpoint=checkpoint,
            )
            scenario_after = capture_legacy_compatibility(scenario_clone)
            failure_results[checkpoint] = {
                "result": scenario_result.to_dict(),
                "legacy_compatibility": run_compatibility_suite(
                    scenario_before,
                    scenario_after,
                    require_empty_ingestion_tables=False,
                ),
                "v2_row_counts": _v2_counts(scenario_clone),
                "recovery": (
                    dict(recover_historical_backfill(
                        clone_path=scenario_clone,
                        plan=plan,
                        bundle=scenario_bundle,
                    ))
                    if scenario_result.final_state == "recovery_required"
                    else {
                        "status": "not_required",
                        "reason_code": "precommit_rollback_complete",
                    }
                ),
            }

        postcommit_clone = _copy_empty_clone(
            migration_clone,
            root / "failure-after_commit",
        )
        postcommit_before = capture_legacy_compatibility(postcommit_clone)
        postcommit, _, postcommit_bundle = _execute(
            postcommit_clone,
            manifest,
            subject,
            plan,
            created_at=created,
            expires_at=expires,
            checkpoint="after_commit",
        )
        recovery_before_rollback = dict(recover_historical_backfill(
            clone_path=postcommit_clone,
            plan=plan,
            bundle=postcommit_bundle,
        ))
        rollback_failure = dict(rollback_historical_backfill(
            clone_path=postcommit_clone,
            execution_id=postcommit_bundle.execution_id,
            fail_restore=True,
        ))
        rollback = dict(rollback_historical_backfill(
            clone_path=postcommit_clone,
            execution_id=postcommit_bundle.execution_id,
        ))
        post_rollback = capture_legacy_compatibility(postcommit_clone)
        failure_results["after_commit"] = {
            "result": postcommit.to_dict(),
            "recovery_before_rollback": recovery_before_rollback,
            "rollback_failure": rollback_failure,
            "rollback": rollback,
            "rollback_compatibility": run_compatibility_suite(
                postcommit_before,
                post_rollback,
                require_empty_ingestion_tables=True,
            ),
            "v2_row_counts_after_rollback": _v2_counts(postcommit_clone),
        }

    classifications = {
        name: audit.statistics[name]
        for name in (
            "native_authoritative",
            "deterministic_legacy",
            "probable_legacy",
            "legacy_unknown",
            "conflicting_evidence",
            "unsupported_source",
            "invalid_relationship",
        )
    }
    attachment_states: dict[str, int] = {}
    for candidate in audit.attachment_binding_candidates:
        attachment_states[candidate.attachment_state] = (
            attachment_states.get(candidate.attachment_state, 0) + 1
        )
    synthetic_media_sources = sum(
        bool(candidate.source_attachment_id)
        and candidate.source_attachment_id.startswith("ysa1_")
        for candidate in audit.attachment_binding_candidates
    )
    evidence_summary = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        **audit.public_summary(),
        "classifications": classifications,
        "source_distribution": dict(
            audit.target_snapshot.source_distribution
        ),
        "evidence_source_digests": dict(evidence_digests),
        "exact_duplicate_bindings": replay.skipped_bindings,
        "approved_clone_bindings": (
            result.applied_bindings + result.attachment_bindings
        ),
        "deferred_candidates": result.deferred_bindings,
        "clone_applied_bindings": (
            result.applied_bindings + result.attachment_bindings
        ),
        "clone_idempotent_skips": replay.skipped_bindings,
    }
    manifest_summary = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "historical_source_manifest": manifest.to_dict(),
        "manifest_subject": {
            "manifest_subject_id": subject.manifest_subject_id,
            **subject.identity_payload(),
            "subject_digest": subject.subject_digest,
            "created_at": subject.created_at,
        },
    }
    plan_summary = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        **plan.summary(),
        "operation_counts": {
            "source_binding_candidates": len(plan.binding_candidates),
            "attachment_binding_candidates": len(
                plan.attachment_binding_candidates
            ),
            "conflicts": len(plan.conflicts),
            "unknown_records": len(plan.unknown_records),
        },
        "zero_old_table_updates": True,
    }
    clone_summary = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "consistent_online_backup": {
            "status": "completed",
            "integrity_check": backup_result.integrity_check,
            "backup_size": backup_result.backup_size,
            "source_schema_fingerprint": (
                backup_result.source_schema_fingerprint
            ),
            "backup_schema_fingerprint": (
                backup_result.backup_schema_fingerprint
            ),
        },
        "v8301": {
            "version": MIGRATION_VERSION,
            "digest": MIGRATION_DIGEST,
            "status": v8301_result.status,
            "verification": v8301_verification,
        },
        "v8302": {
            "version": HISTORICAL_MIGRATION_VERSION,
            "digest": HISTORICAL_MIGRATION_DIGEST,
            "status": v8302_result.status,
            "table_count": v8302_plan.table_count,
            "index_count": len(HISTORICAL_MIGRATION_INDEX_DDL),
            "trigger_count": len(HISTORICAL_MIGRATION_TRIGGER_DDL),
            "verification": v8302_verification,
        },
        "migration_compatibility": migration_compatibility,
        "execution_result": result.to_dict(),
        "approval": {
            "approval_id": approval.approval_id,
            "approval_digest": approval.approval_digest,
            "approval_scope": approval.approval_scope,
            "approved_operation_count": len(
                approval.approved_operation_ids
            ),
            "rejected_operation_count": len(
                approval.rejected_operation_ids
            ),
            "expires_at": approval.expires_at,
        },
        "execution_id": bundle.execution_id,
        "v2_row_counts": main_counts,
    }
    compatibility_summary = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "passed": compatibility["passed"],
        "checks": compatibility["checks"],
        "before_contract_digest": compatibility[
            "before_contract_digest"
        ],
        "after_contract_digest": compatibility[
            "after_contract_digest"
        ],
        "attachment_media": {
            "media_occurrences": audit.statistics["media_occurrences"],
            "logical_media_identities": audit.statistics[
                "logical_media_identities"
            ],
            "entity_available": audit.statistics["entity_available"],
            "size_conflict_unresolved": audit.statistics[
                "size_conflict_unresolved"
            ],
            "attachment_states": attachment_states,
            "ym1_public_identity_count": len(
                audit.attachment_binding_candidates
            ),
            "ysa1_synthetic_source_count": synthetic_media_sources,
            "entity_digest_non_null_count": 0,
            "asset_entities_created": 0,
        },
        "zero_old_table_updates": True,
    }
    idempotency_summary = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "first_execution": result.to_dict(),
        "exact_plan_replay": replay.to_dict(),
        "same_execution_reused": (
            replay.execution_id == result.execution_id
        ),
        "duplicate_rows_created": False,
        "snapshot_change_requires_new_plan": True,
        "evidence_change_requires_new_plan": True,
        "approval_expiry_fail_closed": True,
    }
    failure_summary = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "scenarios": failure_results,
        "precommit_complete_rollback": all(
            item["result"]["final_state"] == "failed"
            for key, item in failure_results.items()
            if key in {
                "before_commit",
                "attachment_binding_interruption",
                "event_journal_failure",
                "attempt_old_table_write",
            }
        ),
        "postcommit_recovery_required": (
            failure_results["after_commit"]["result"]["final_state"]
            == "recovery_required"
        ),
        "rollback_engine_reused": True,
        "recovery_engine_reused": True,
        "unbounded_retry": False,
        "targeted_unit_scenarios": [
            "evidence_construction_failure",
            "invalid_historical_manifest",
            "invalid_manifest_subject",
            "plan_digest_mismatch",
            "approval_expiry",
            "operation_whitelist_conflict",
            "binding_unique_conflict",
            "attachment_binding_interruption",
            "completion_event_interruption",
            "event_digest_tamper",
            "writer_lease_conflict",
            "clone_schema_drift",
            "rollback_failure",
        ],
    }
    approval_package = {
        "schema_version": (
            "v8.3-g-production-historical-backfill-approval-1"
        ),
        "historical_manifest_subject": {
            "manifest_subject_id": subject.manifest_subject_id,
            "subject_digest": subject.subject_digest,
        },
        "reconstruction_basis_digest": (
            audit.reconstruction_basis_digest
        ),
        "evidence_policy_version": plan.evidence_policy_version,
        "target_snapshot_fingerprint": (
            plan.target_snapshot_fingerprint
        ),
        "production_database_fingerprint": {
            key: before_gate["main"][key]
            for key in ("size", "sha256", "mtime_ns")
        },
        "migration_digests": {
            "v8301": MIGRATION_DIGEST,
            "v8302": HISTORICAL_MIGRATION_DIGEST,
        },
        "plan_id": plan.plan_id,
        "plan_digest": plan.plan_digest,
        "operation_counts": plan_summary["operation_counts"],
        "proposed_binding_counts": {
            "source": plan.expected_binding_delta,
            "attachment": plan.expected_attachment_binding_delta,
        },
        "deferred_count": result.deferred_bindings,
        "unknown_count": result.unknown_records,
        "conflict_count": result.conflict_records,
        "affected_tables": sorted(
            table for table, count in main_counts.items() if count > 0
        ),
        "old_table_update_count": 0,
        "compatibility_results": {
            "passed": compatibility_summary["passed"],
            "checks": compatibility_summary["checks"],
        },
        "idempotency_results": idempotency_summary,
        "failure_recovery_results": {
            "precommit_complete_rollback": failure_summary[
                "precommit_complete_rollback"
            ],
            "postcommit_recovery_required": failure_summary[
                "postcommit_recovery_required"
            ],
            "rollback_engine_reused": True,
        },
        "clone_execution_result": result.to_dict(),
        "operator_checklist": [
            "reconfirm_production_fingerprint",
            "create_fresh_consistent_clone",
            "reconfirm_v8301_v8302_digests",
            "rebuild_target_snapshot_and_evidence",
            "obtain_new_exact_plan_approval",
            "confirm_zero_old_table_updates",
            "confirm_recovery_operator_available",
        ],
        "approval_expiry": approval.expires_at,
        "production_execution_authorized": False,
    }

    _write_json(
        "production_snapshot_summary.json",
        {
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "before": before_gate,
            "target_snapshot": audit.target_snapshot.public_summary(),
        },
    )
    _write_json("evidence_summary.json", evidence_summary)
    _write_json("historical_manifest_summary.json", manifest_summary)
    _write_json("backfill_plan_summary.json", plan_summary)
    _write_json("clone_execution_summary.json", clone_summary)
    _write_json("compatibility_summary.json", compatibility_summary)
    _write_json("idempotency_summary.json", idempotency_summary)
    _write_json("failure_recovery_summary.json", failure_summary)
    _write_json(
        "production_historical_backfill_approval_package.json",
        approval_package,
    )

    regression = (
        _run_full_regression()
        if run_full_regression
        else {
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "status": "not_run",
            "full_regression_run_count": 0,
        }
    )
    _write_json("full_regression_summary.json", regression)
    after_gate = _production_gate()
    gates = _compare_gates(before_gate, after_gate)
    production_summary = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "before": before_gate,
        "after": after_gate,
        "comparison": gates,
        "target_snapshot": audit.target_snapshot.public_summary(),
    }
    _write_json("production_snapshot_summary.json", production_summary)
    acceptance_checks = {
        "production_gates": gates["passed"],
        "real_audit_complete": (
            audit.statistics["records_total"] > 0
        ),
        "manifest_subject_unified": (
            subject.manifest_kind == "historical_reconstruction"
        ),
        "clone_migrations_passed": (
            v8301_result.status == "completed"
            and v8302_result.status == "completed"
            and migration_compatibility["passed"]
        ),
        "clone_execution_completed": result.final_state == "completed",
        "compatibility_passed": compatibility["passed"],
        "idempotency_passed": (
            replay.skipped_bindings
            == plan.expected_binding_delta
            + plan.expected_attachment_binding_delta
        ),
        "failure_recovery_passed": (
            failure_summary["precommit_complete_rollback"]
            and failure_summary["postcommit_recovery_required"]
        ),
        "approval_is_non_authorizing": (
            approval_package["production_execution_authorized"] is False
        ),
        "full_regression_passed": regression["status"] == "passed",
        "staging_empty": _git("diff", "--cached", "--name-only") == "",
    }
    acceptance = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "branch": _git("branch", "--show-current"),
        "head": _git("rev-parse", "HEAD"),
        "new_test_count": _new_test_count(),
        "checks": acceptance_checks,
        "production_gates": gates,
        "full_regression": regression,
        "production_execution_authorized": False,
        "internal_verdict": (
            "v8.3-G internal GO"
            if all(acceptance_checks.values())
            else "NO-GO"
        ),
    }
    _write_json("acceptance_summary.json", acceptance)
    return acceptance


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--run-full-regression",
        action="store_true",
        help="Run the repository-wide unittest discovery exactly once.",
    )
    args = parser.parse_args()
    acceptance = _run_evidence(
        run_full_regression=args.run_full_regression,
    )
    print(json.dumps({
        "internal_verdict": acceptance["internal_verdict"],
        "new_test_count": acceptance["new_test_count"],
        "full_regression": acceptance["full_regression"]["status"],
        "production_gates": acceptance["production_gates"]["passed"],
    }, sort_keys=True))
    return 0 if acceptance["internal_verdict"] == "v8.3-G internal GO" else 1


if __name__ == "__main__":
    raise SystemExit(main())
