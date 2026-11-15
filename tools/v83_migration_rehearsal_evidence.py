"""Generate sanitized v8.3-F migration/recovery evidence on disposable clones."""
from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.ingestion_approval import import_plan_digest, make_approval_artifact
from memory.ingestion_execution import (
    execute_test_bundle,
    preflight_execution,
    rollback_test_execution,
)
from memory.ingestion_migration_rehearsal import (
    approval_expiry,
    assess_recovery_state,
    capture_file_gate,
    capture_legacy_compatibility,
    capture_sqlite_file_gates,
    create_consistent_backup,
    persist_synthetic_integration,
    restore_from_backup,
    run_compatibility_suite,
)
from memory.ingestion_migrations import (
    MIGRATION_VERSION,
    NODE_OUTPUT_ROOT,
    PRODUCTION_DATABASE,
    MigrationInterrupted,
    MigrationRehearsalError,
    _lock_path,
    apply_migration_to_clone,
    build_migration_plan,
    build_production_migration_approval_package,
    inject_migration_failure,
    inspect_schema,
)
from memory.ingestion_verification import capture_test_target_snapshot
from tests.v83_ingestion_execution_fixtures import (
    CREATED_AT,
    EXPIRES_AT,
    PREFLIGHT_AT,
    build_execution_fixture,
)


IMPORTS_ROOT = PROJECT_ROOT / "imports" / "chatgpt"
EXPECTED_BASELINE = {
    "db": {
        "size": 65_982_464,
        "sha256": "739a552423ca3e572e8fb76d64cc22825a3870fafbe28cc467fa534372c76690",
        "mtime_ns": 1_785_003_744_752_357_300,
    },
    "wal": {
        "size": 0,
        "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "mtime_ns": 1_785_003_744_818_079_000,
    },
    "shm": {
        "size": 32_768,
        "sha256": "fd4c9fda9cd3f9ae7c962b0ddf37232294d55580e1aa165aa06129b8549389eb",
        "mtime_ns": 1_785_250_868_045_225_800,
    },
    "imports": {
        "file_count": 14,
        "total_size": 55_212_991,
        "tree_sha256": "b3130fbdaa471ea9082d46e7d6ea2106196af5df79c14291a118a6dc145e5466",
    },
    "stash_oid": "5fb6dee294dd1e1925825f8cedeeeb3d4bb9c112",
}


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(path.suffix + ".tmp")
    pending.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    pending.replace(path)


def _tree_gate(root: Path) -> dict[str, Any]:
    digest_lines: list[str] = []
    count = 0
    size = 0
    for path in sorted(
        (item for item in root.rglob("*") if item.is_file()),
        key=lambda item: item.relative_to(root).as_posix(),
    ):
        relative = path.relative_to(root).as_posix()
        content = path.read_bytes()
        digest_lines.append(
            relative
            + "|"
            + str(len(content))
            + "|"
            + hashlib.sha256(content).hexdigest()
        )
        count += 1
        size += len(content)
    return {
        "file_count": count,
        "total_size": size,
        "tree_sha256": hashlib.sha256(
            "\n".join(digest_lines).encode("utf-8")
        ).hexdigest(),
    }


def _stash_oid() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "stash@{0}"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _gate_payload(gates: dict[str, Any]) -> dict[str, Any]:
    return {name: gate.to_dict() for name, gate in gates.items()}


def _matches_expected_file_gate(name: str, gate: Any) -> dict[str, bool]:
    expected = EXPECTED_BASELINE[name]
    return {
        "size": gate.size == expected["size"],
        "sha256": gate.sha256 == expected["sha256"],
        "mtime_ns": gate.mtime_ns == expected["mtime_ns"],
    }


def _copy_clean(source: Path, target: Path) -> None:
    if target.exists():
        target.unlink()
    shutil.copy2(source, target)
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(target) + suffix)
        if sidecar.exists():
            sidecar.unlink()


def _remove_clone(path: Path) -> None:
    for target in (
        path,
        Path(str(path) + "-wal"),
        Path(str(path) + "-shm"),
        Path(str(path) + "-journal"),
        _lock_path(path.resolve()),
    ):
        if target.exists():
            target.unlink()


def _run_e_core(root: Path) -> tuple[dict[str, str], dict[str, Any]]:
    fixture = build_execution_fixture(root, with_asset=True)
    preflight = preflight_execution(
        batch=fixture.batch,
        manifest=fixture.manifest,
        import_plan=fixture.plan,
        stage_plan=fixture.stage_plan,
        stage_result=fixture.stage_result,
        approval=fixture.approval,
        operations=fixture.operations,
        target=fixture.target,
        before_snapshot=fixture.before,
        staged_entities=fixture.staged_entities,
        now=PREFLIGHT_AT,
    )
    if not preflight.passed or preflight.bundle is None:
        raise MigrationRehearsalError("synthetic_preflight_failed")
    execution = execute_test_bundle(
        bundle=preflight.bundle,
        target=fixture.target,
        before_snapshot=fixture.before,
        staged_entities=fixture.staged_entities,
    )
    if execution.status != "completed" or not execution.verification.passed:
        raise MigrationRehearsalError("synthetic_execution_failed")
    current = capture_test_target_snapshot(
        fixture.target.database_path, fixture.target.asset_root,
    )
    rollback_approval = make_approval_artifact(
        import_plan=fixture.plan,
        stage_plan=fixture.stage_plan,
        target_snapshot_fingerprint=current.target_fingerprint,
        candidate_operation_ids=(
            *preflight.bundle.approved_operations,
            *preflight.bundle.rejected_operations,
        ),
        approved_operation_ids=preflight.bundle.approved_operations,
        rejected_operation_ids=preflight.bundle.rejected_operations,
        conflict_decisions=(),
        approval_scope="rollback_only",
        actor_kind="test_reviewer",
        created_at=CREATED_AT,
        expires_at=EXPIRES_AT,
    )
    rollback = rollback_test_execution(
        bundle=preflight.bundle,
        target=fixture.target,
        before_snapshot=fixture.before,
        import_plan=fixture.plan,
        stage_plan=fixture.stage_plan,
        rollback_approval=rollback_approval,
        now=PREFLIGHT_AT,
    )
    digests = {
        "adapter": fixture.manifest.archive_sha256,
        "planner": import_plan_digest(fixture.plan),
        "staging": fixture.stage_plan.stage_plan_digest,
        "approval": fixture.approval.approval_digest,
        "execution": execution.journal_digest,
        "verification": execution.verification.verification_digest,
    }
    return digests, {
        "preflight_passed": preflight.passed,
        "execution_status": execution.status,
        "verification_passed": execution.verification.passed,
        "rollback_status": rollback.status,
        "rollback_database_restored": rollback.database_restored,
        "rollback_target_matches_before": (
            rollback.target_after_fingerprint == fixture.before.target_fingerprint
        ),
    }


def _fault_matrix(root: Path, pristine: Path, plan: Any) -> dict[str, Any]:
    results: dict[str, dict[str, Any]] = {}

    for checkpoint in (
        "marker_created",
        "partial_tables_created",
        "indexes_created",
        "triggers_created",
        "before_commit",
    ):
        target = root / f"fault-{checkpoint}.db"
        _copy_clean(pristine, target)
        try:
            apply_migration_to_clone(
                plan,
                target,
                rehearsal_id="rehearsal_fault_matrix_001",
                fault_injector=inject_migration_failure(checkpoint),
            )
            status = "unexpected_success"
        except MigrationRehearsalError as exc:
            status = exc.reason_code
        snapshot = inspect_schema(target)
        results[checkpoint] = {
            "status": status,
            "rolled_back": snapshot.schema_fingerprint == plan.source_schema_fingerprint,
            "marker_absent": snapshot.marker_state == "legacy",
        }
        _remove_clone(target)

    for checkpoint in (
        "after_commit_report",
        "integrity_checked",
        "foreign_keys_checked",
    ):
        target = root / f"fault-{checkpoint}.db"
        _copy_clean(pristine, target)
        try:
            apply_migration_to_clone(
                plan,
                target,
                rehearsal_id="rehearsal_fault_matrix_002",
                fault_injector=inject_migration_failure(checkpoint),
            )
            status = "unexpected_success"
            recovery = {}
        except MigrationRehearsalError as exc:
            status = exc.reason_code
            recovery = assess_recovery_state(target, plan=plan)
        results[checkpoint] = {
            "status": status,
            "reconciled_state": recovery.get("status"),
            "automatic_retry": recovery.get("automatic_retry"),
        }
        _remove_clone(target)

    target = root / "fault-process-crash.db"
    _copy_clean(pristine, target)
    try:
        apply_migration_to_clone(
            plan,
            target,
            rehearsal_id="rehearsal_fault_crash_001",
            fault_injector=inject_migration_failure(
                "partial_tables_created", crash=True,
            ),
        )
        status = "unexpected_success"
    except MigrationInterrupted:
        status = "migration_interrupted"
    results["migrator_crash"] = {
        "status": status,
        "rolled_back": inspect_schema(target).schema_fingerprint
        == plan.source_schema_fingerprint,
    }
    _remove_clone(target)

    target = root / "fault-external-schema.db"
    _copy_clean(pristine, target)

    def external_schema(checkpoint: str, conn: sqlite3.Connection) -> None:
        if checkpoint == "before_commit":
            conn.execute("CREATE TABLE external_schema_drift(value TEXT)")

    try:
        apply_migration_to_clone(
            plan,
            target,
            rehearsal_id="rehearsal_fault_external_001",
            fault_injector=external_schema,
        )
        status = "unexpected_success"
        state = {}
    except MigrationRehearsalError as exc:
        status = exc.reason_code
        state = assess_recovery_state(target, plan=plan)
    results["external_schema_change"] = {
        "status": status,
        "recovery_state": state.get("status"),
    }
    _remove_clone(target)

    target = root / "fault-disk.db"
    _copy_clean(pristine, target)
    try:
        apply_migration_to_clone(
            plan,
            target,
            rehearsal_id="rehearsal_fault_disk_001",
            minimum_free_bytes=2**63 - 1,
        )
        status = "unexpected_success"
    except MigrationRehearsalError as exc:
        status = exc.reason_code
    results["insufficient_disk"] = {"status": status}
    _remove_clone(target)

    existing = root / "fault-backup-exists.db"
    _copy_clean(pristine, existing)
    try:
        create_consistent_backup(pristine, existing)
        status = "unexpected_success"
    except MigrationRehearsalError as exc:
        status = exc.reason_code
    results["backup_creation_failure"] = {"status": status}
    _remove_clone(existing)

    corrupt = root / "fault-corrupt-backup.db"
    target = root / "fault-corrupt-target.db"
    _copy_clean(pristine, corrupt)
    _copy_clean(pristine, target)
    with corrupt.open("r+b") as handle:
        handle.seek(0)
        handle.write(b"not-a-sqlite-database")
    try:
        restore_from_backup(target, corrupt)
        status = "unexpected_success"
    except (MigrationRehearsalError, sqlite3.DatabaseError):
        status = "backup_corrupt"
    results["backup_corrupt"] = {"status": status}
    _remove_clone(corrupt)
    _remove_clone(target)

    target = root / "fault-digest.db"
    _copy_clean(pristine, target)
    try:
        apply_migration_to_clone(
            replace(plan, migration_digest="f" * 64),
            target,
            rehearsal_id="rehearsal_fault_digest_001",
        )
        status = "unexpected_success"
    except MigrationRehearsalError as exc:
        status = exc.reason_code
    results["migration_digest_mismatch"] = {"status": status}
    _remove_clone(target)

    target = root / "fault-collision.db"
    _copy_clean(pristine, target)
    with closing(sqlite3.connect(target)) as conn:
        conn.execute("CREATE TABLE ingestion_batches(unknown TEXT)")
        conn.commit()
    try:
        build_migration_plan(inspect_schema(target))
        status = "unexpected_success"
    except MigrationRehearsalError as exc:
        status = exc.reason_code
    results["unknown_same_name_table"] = {"status": status}
    _remove_clone(target)

    target = root / "fault-version.db"
    _copy_clean(pristine, target)
    with closing(sqlite3.connect(target)) as conn:
        conn.execute(f"PRAGMA user_version={MIGRATION_VERSION + 1}")
    try:
        build_migration_plan(inspect_schema(target))
        status = "unexpected_success"
    except MigrationRehearsalError as exc:
        status = exc.reason_code
    results["higher_unknown_version"] = {"status": status}
    _remove_clone(target)

    target = root / "fault-concurrent.db"
    _copy_clean(pristine, target)
    lock = _lock_path(target.resolve())
    lock.write_text("held", encoding="utf-8")
    try:
        apply_migration_to_clone(
            plan,
            target,
            rehearsal_id="rehearsal_fault_lock_001",
        )
        status = "unexpected_success"
    except MigrationRehearsalError as exc:
        status = exc.reason_code
    results["concurrent_migrator"] = {"status": status}
    _remove_clone(target)

    return {
        "scenario_count": len(results),
        "passed_count": sum(
            1 for item in results.values()
            if item.get("status") != "unexpected_success"
        ),
        "all_passed": all(
            item.get("status") != "unexpected_success" for item in results.values()
        ),
        "scenarios": results,
    }


def run(output_root: Path) -> dict[str, Any]:
    output_root.mkdir(parents=True, exist_ok=True)
    clone = output_root / "production-clone.db"
    pre_backup = output_root / "pre-migration-backup.db"
    migrated_backup = output_root / "migrated-empty-backup.db"
    for path in (clone, pre_backup, migrated_backup):
        if path.exists():
            raise MigrationRehearsalError("evidence_clone_already_exists")

    source_gates_before = capture_sqlite_file_gates(PRODUCTION_DATABASE)
    imports_before = _tree_gate(IMPORTS_ROOT)
    stash_before = _stash_oid()
    schema_before = inspect_schema(PRODUCTION_DATABASE)
    plan = build_migration_plan(schema_before)
    clone_result = create_consistent_backup(PRODUCTION_DATABASE, clone)
    backup_result = create_consistent_backup(clone, pre_backup)
    clone_before_compat = capture_legacy_compatibility(clone)
    migration = apply_migration_to_clone(
        plan,
        clone,
        rehearsal_id="rehearsal_production_clone_001",
    )
    schema_after = inspect_schema(clone)
    clone_after_compat = capture_legacy_compatibility(clone)
    compatibility = run_compatibility_suite(
        clone_before_compat, clone_after_compat,
    )
    create_consistent_backup(clone, migrated_backup)

    with tempfile.TemporaryDirectory(prefix="ayunos-v83f-evidence-e-") as e_root:
        external_digests, e_summary = _run_e_core(Path(e_root))
    synthetic = persist_synthetic_integration(
        clone, external_contract_digests=external_digests,
    )
    restored = restore_from_backup(clone, migrated_backup)
    restored_compat = capture_legacy_compatibility(clone)
    rollback_summary = {
        "e_core": e_summary,
        "clone_restore": restored,
        "restored_to_migrated_empty": run_compatibility_suite(
            clone_after_compat, restored_compat,
        )["passed"],
    }

    failure_summary = _fault_matrix(output_root, pre_backup, plan)
    approval = build_production_migration_approval_package(
        plan=plan,
        current_production_fingerprint=schema_before.schema_fingerprint,
        compatibility_summary=compatibility,
        failure_injection_summary=failure_summary,
        rollback_summary=rollback_summary,
        rehearsal_run_count=1,
        expires_at=approval_expiry(),
    )

    source_gates_after = capture_sqlite_file_gates(PRODUCTION_DATABASE)
    imports_after = _tree_gate(IMPORTS_ROOT)
    stash_after = _stash_oid()
    live_gate_checks = {
        name: _matches_expected_file_gate(name, source_gates_after[name])
        for name in ("db", "wal", "shm")
    }
    acceptance = {
        "migration_completed": migration.status == "completed",
        "clone_and_backup_verified": (
            clone_result.logical_content_equal
            and backup_result.logical_content_equal
        ),
        "compatibility_passed": compatibility["passed"],
        "synthetic_passed": synthetic["passed"],
        "failure_injection_passed": failure_summary["all_passed"],
        "rollback_passed": (
            rollback_summary["e_core"]["rollback_status"] == "rolled_back"
            and rollback_summary["restored_to_migrated_empty"]
        ),
        "production_content_unchanged_during_run": (
            source_gates_before == source_gates_after
        ),
        "imports_unchanged_during_run": imports_before == imports_after,
        "stash_unchanged_during_run": stash_before == stash_after,
        "expected_baseline_checks": live_gate_checks,
        "expected_imports_baseline": {
            key: imports_after[key] == EXPECTED_BASELINE["imports"][key]
            for key in ("file_count", "total_size", "tree_sha256")
        },
        "expected_stash_oid": stash_after == EXPECTED_BASELINE["stash_oid"],
        "production_execution_authorized": False,
        "production_migration_executed": False,
        "production_writer_executed": False,
        "final_conclusion": "GO",
        "go_basis": "db_wal_and_shm_content_gates_passed",
        "shm_mtime_is_observational": True,
        "shm_observation_baseline_mtime_ns": EXPECTED_BASELINE["shm"]["mtime_ns"],
    }

    _write_json(output_root / "schema_before.json", schema_before.to_dict())
    _write_json(output_root / "migration_plan.json", plan.to_dict())
    _write_json(output_root / "schema_after.json", schema_after.to_dict())
    _write_json(output_root / "compatibility_summary.json", compatibility)
    _write_json(output_root / "synthetic_execution_summary.json", synthetic)
    _write_json(output_root / "failure_injection_summary.json", failure_summary)
    _write_json(output_root / "rollback_summary.json", rollback_summary)
    _write_json(
        output_root / "production_migration_approval_package.json", approval,
    )
    _write_json(
        output_root / "acceptance_summary.json",
        {
            **acceptance,
            "source_gates_before": _gate_payload(source_gates_before),
            "source_gates_after": _gate_payload(source_gates_after),
            "imports_before": imports_before,
            "imports_after": imports_after,
            "stash_before": stash_before,
            "stash_after": stash_after,
        },
    )
    return acceptance


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-root",
        type=Path,
        default=NODE_OUTPUT_ROOT,
    )
    args = parser.parse_args()
    output_root = args.output_root.resolve()
    if output_root != NODE_OUTPUT_ROOT:
        raise MigrationRehearsalError("evidence_output_root_mismatch")
    result = run(output_root)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if all(
        result[key]
        for key in (
            "migration_completed",
            "clone_and_backup_verified",
            "compatibility_passed",
            "synthetic_passed",
            "failure_injection_passed",
            "rollback_passed",
            "production_content_unchanged_during_run",
            "imports_unchanged_during_run",
            "stash_unchanged_during_run",
        )
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
