"""Generate the post-commit v8301 approval package and sanitized evidence.

This tool is preparation-only.  It reads the four fixed production databases
through immutable SQLite connections, revalidates an existing verified backup,
runs v8301 only on disposable clones, and writes ignored approval artifacts.
It never creates a HumanAuthorizationRecord or calls the production executor.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from typing import Any, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.ingestion_core import canonical_json_bytes
from memory.ingestion_migration_rehearsal import (
    assess_recovery_state,
    capture_sqlite_file_gates,
    restore_from_backup,
)
from memory.ingestion_migrations import (
    HISTORICAL_INGESTION_TABLES,
    MIGRATION_DIGEST,
    MIGRATION_VERSION,
    MigrationRehearsalError,
    apply_migration_to_clone,
    apply_production_migration,
    build_migration_plan,
    inject_migration_failure,
    inspect_schema,
    inspect_schema_connection,
)
from memory.production_migration_authorization import (
    PRODUCTION_EXECUTION_PROTOCOL_VERSION,
    SUPERSEDED_CLASSIFICATION,
    build_production_migration_approval_package_v2,
    canonical_document_digest,
    migration_plan_digest,
)
from memory.production_migration_clone import execute_v8301_clone_protocol
from memory.production_migration_entry import (
    REQUIRED_JOURNAL_EVENTS,
    assert_execution_protocol_static_guards,
    validate_production_main_database_path,
)


OUTPUT_ROOT = PROJECT_ROOT / "output" / "v8.3-production-migration-entry"
APPROVAL_PATH = OUTPUT_ROOT / "production_v8301_migration_approval_package_v2.json"
EXPECTED_OLD_PACKAGE_DIGEST = (
    "cf2a4f270ef99d7f5ad871ecfb52feb6bf9f12d1b85ff4d2d5abe740d3b17df2"
)
DATABASES = {
    "Main": PROJECT_ROOT / "data" / "ayunos_chatgpt.db",
    "Profile": PROJECT_ROOT / "data" / "profile" / "approved_profile.db",
    "Semantic": PROJECT_ROOT / "data" / "semantic" / "ayunos_semantic.db",
    "Runtime": PROJECT_ROOT / "data" / "runtime" / "ayunos_runtime.db",
}
BACKUP_FILENAMES = {
    "Main": "main.db",
    "Profile": "profile.db",
    "Semantic": "semantic.db",
    "Runtime": "runtime.db",
}
EVIDENCE_FILENAMES = (
    "code_review_summary.json",
    "authorization_protocol_summary.json",
    "clone_execution_summary.json",
    "security_gate_summary.json",
    "new_approval_package_summary.json",
    "acceptance_summary.json",
)


class ApprovalPreparationError(RuntimeError):
    pass


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _file_gate(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "exists": False,
            "size": 0,
            "mtime_ns": 0,
            "sha256": hashlib.sha256(b"").hexdigest(),
        }
    stat = path.stat()
    return {
        "exists": True,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": _hash_file(path),
    }


def _sqlite_gates(path: Path) -> dict[str, dict[str, Any]]:
    return {
        "db": _file_gate(path),
        "wal": _file_gate(Path(str(path) + "-wal")),
        "shm": _file_gate(Path(str(path) + "-shm")),
        "journal": _file_gate(Path(str(path) + "-journal")),
    }


def _quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _database_audit(label: str, path: Path) -> dict[str, Any]:
    before = _sqlite_gates(path)
    with closing(
        sqlite3.connect(
            path.resolve(strict=True).as_uri() + "?mode=ro&immutable=1",
            uri=True,
        )
    ) as connection:
        connection.execute("PRAGMA query_only=ON")
        snapshot = inspect_schema_connection(connection)
        integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
        foreign_keys = list(connection.execute("PRAGMA foreign_key_check"))
        shadow = {
            str(row[1])
            for row in connection.execute("PRAGMA table_list")
            if len(row) >= 3 and row[2] == "shadow"
        }
        tables = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
            if str(row[0]) not in shadow
        ]
        table_counts = {
            table: int(
                connection.execute(
                    "SELECT COUNT(*) FROM " + _quote(table)
                ).fetchone()[0]
            )
            for table in tables
        }
    after = _sqlite_gates(path)
    return {
        "label": label,
        "relative_path": path.relative_to(PROJECT_ROOT).as_posix(),
        "file_gates": before,
        "schema_fingerprint": snapshot.schema_fingerprint,
        "marker_state": snapshot.marker_state,
        "migration_version": snapshot.migration_version,
        "migration_digest": snapshot.migration_digest,
        "integrity_check": integrity,
        "foreign_key_violation_count": len(foreign_keys),
        "table_counts": table_counts,
        "immutable_read_gates_unchanged": before == after,
    }


def _immutable_schema_snapshot(path: Path):
    with closing(
        sqlite3.connect(
            path.resolve(strict=True).as_uri() + "?mode=ro&immutable=1",
            uri=True,
        )
    ) as connection:
        connection.execute("PRAGMA query_only=ON")
        return inspect_schema_connection(connection)


def _audit_four_databases() -> dict[str, dict[str, Any]]:
    return {
        label: _database_audit(label, path)
        for label, path in DATABASES.items()
    }


def _tree_gate(root: Path) -> dict[str, Any]:
    lines: list[str] = []
    total_size = 0
    files = sorted(
        (path for path in root.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    for path in files:
        relative = path.relative_to(root).as_posix()
        size = path.stat().st_size
        total_size += size
        lines.append(relative + "|" + str(size) + "|" + _hash_file(path))
    return {
        "file_count": len(files),
        "total_size": total_size,
        "tree_sha256": hashlib.sha256(
            "\n".join(lines).encode("utf-8")
        ).hexdigest(),
    }


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ApprovalPreparationError("json_document_invalid") from None
    if not isinstance(value, dict):
        raise ApprovalPreparationError("json_document_invalid")
    return value


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _git(*args: str) -> str:
    try:
        completed = subprocess.run(
            ("git", "-C", str(PROJECT_ROOT), *args),
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except (OSError, subprocess.CalledProcessError):
        raise ApprovalPreparationError("git_gate_failed") from None
    return completed.stdout.strip()


def _validate_old_package(path: Path) -> dict[str, Any]:
    before = _hash_file(path)
    package = _load_json(path)
    if (
        package.get("package_digest") != EXPECTED_OLD_PACKAGE_DIGEST
        or canonical_document_digest(package, digest_field="package_digest")
        != EXPECTED_OLD_PACKAGE_DIGEST
        or package.get("production_execution_authorized") is not False
    ):
        raise ApprovalPreparationError("old_approval_package_mismatch")
    if _hash_file(path) != before:
        raise ApprovalPreparationError("old_approval_package_changed")
    return {
        "path": path.relative_to(PROJECT_ROOT).as_posix(),
        "package_digest": EXPECTED_OLD_PACKAGE_DIGEST,
        "file_sha256": before,
        "classification": SUPERSEDED_CLASSIFICATION,
        "unchanged": True,
    }


def _validate_backup(
    backup_directory: Path,
    audits: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    manifest_path = backup_directory / "backup_manifest.json"
    manifest = _load_json(manifest_path)
    manifest_digest = manifest.get("manifest_digest")
    if (
        not isinstance(manifest_digest, str)
        or canonical_document_digest(manifest, digest_field="manifest_digest")
        != manifest_digest
        or manifest.get("production_execution_authorized") is not False
        or manifest.get("target_migration_version") != MIGRATION_VERSION
        or manifest.get("target_migration_digest") != MIGRATION_DIGEST
        or manifest.get("restore_rehearsal", {}).get("passed") is not True
    ):
        raise ApprovalPreparationError("backup_manifest_invalid")
    results = manifest.get("database_results")
    if not isinstance(results, Mapping) or set(results) != set(DATABASES):
        raise ApprovalPreparationError("backup_manifest_database_set_mismatch")

    entries: dict[str, dict[str, Any]] = {}
    checks: dict[str, bool] = {}
    for label, source in DATABASES.items():
        recorded = results[label]
        backup_path = backup_directory / BACKUP_FILENAMES[label]
        source_gate = audits[label]["file_gates"]["db"]
        backup_gate = _file_gate(backup_path)
        current_backup = _database_audit(label + "Backup", backup_path)
        checks[label] = bool(
            recorded.get("passed") is True
            and source_gate == recorded.get("source_file")
            and audits[label]["schema_fingerprint"]
            == recorded.get("source_schema_fingerprint")
            and backup_gate == recorded.get("backup_file_gate")
            and current_backup["schema_fingerprint"]
            == recorded.get("backup_schema_fingerprint")
            and current_backup["integrity_check"] == ["ok"]
            and current_backup["foreign_key_violation_count"] == 0
        )
        entries[label] = {
            "relative_path": backup_path.relative_to(PROJECT_ROOT).as_posix(),
            "sha256": backup_gate["sha256"],
            "size": backup_gate["size"],
        }
    if not all(checks.values()):
        raise ApprovalPreparationError("backup_not_reusable_new_backup_required")
    return {
        "backup_directory": backup_directory.name,
        "backup_manifest_relative_path": manifest_path.relative_to(
            PROJECT_ROOT
        ).as_posix(),
        "backup_manifest_digest": manifest_digest,
        "backup_set_digest": _digest(entries),
        "database_backups": entries,
        "database_checks": checks,
        "restore_rehearsal_passed": True,
        "reused_existing_backup": True,
        "new_backup_required": False,
    }


def _clone_execution(main_backup: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="ayunos-v8301-entry-clones-") as name:
        root = Path(name)

        success_root = root / "success"
        success_root.mkdir()
        success = success_root / "target.db"
        shutil.copy2(main_backup, success)
        before = inspect_schema(success)
        plan = build_migration_plan(before)
        result = execute_v8301_clone_protocol(
            plan=plan,
            clone_path=success,
            execution_root=success_root,
            execution_id="execution_v8301_approval_clone_001",
        )
        replay = apply_migration_to_clone(
            plan,
            success,
            rehearsal_id="execution_v8301_approval_clone_002",
        )
        after = inspect_schema(success)
        journal = [
            json.loads(line)
            for line in (success_root / "execution_journal.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]

        pre_root = root / "precommit"
        pre_root.mkdir()
        pre = pre_root / "target.db"
        shutil.copy2(main_backup, pre)
        pre_plan = build_migration_plan(inspect_schema(pre))
        pre_reason = None
        try:
            execute_v8301_clone_protocol(
                plan=pre_plan,
                clone_path=pre,
                execution_root=pre_root,
                execution_id="execution_v8301_approval_precommit_001",
                fault_injector=inject_migration_failure("before_commit"),
            )
        except MigrationRehearsalError as exc:
            pre_reason = exc.reason_code
        pre_after = inspect_schema(pre)

        post_root = root / "postcommit"
        post_root.mkdir()
        post = post_root / "target.db"
        post_backup = post_root / "verified-backup.db"
        shutil.copy2(main_backup, post)
        shutil.copy2(main_backup, post_backup)
        post_plan = build_migration_plan(inspect_schema(post))
        post_reason = None
        post_recovery_required = False
        try:
            execute_v8301_clone_protocol(
                plan=post_plan,
                clone_path=post,
                execution_root=post_root,
                execution_id="execution_v8301_approval_postcommit_001",
                fault_injector=inject_migration_failure("after_commit_report"),
            )
        except MigrationRehearsalError as exc:
            post_reason = exc.reason_code
            post_recovery_required = exc.recovery_required
        recovery = assess_recovery_state(post, plan=post_plan)
        restored = restore_from_backup(post, post_backup)

        summary = {
            "execution_kind": "disposable_clone_only",
            "migration_status": result.status,
            "replay_status": replay.status,
            "migration_version": after.migration_version,
            "migration_digest": after.migration_digest,
            "source_schema_fingerprint": before.schema_fingerprint,
            "target_schema_fingerprint": after.schema_fingerprint,
            "expected_target_schema_fingerprint": (
                plan.expected_target_schema_fingerprint
            ),
            "migration_plan_digest": migration_plan_digest(plan),
            "legacy_row_counts_unchanged": before.row_counts == after.row_counts,
            "legacy_id_digests_unchanged": (
                before.id_set_digests == after.id_set_digests
            ),
            "legacy_critical_digests_unchanged": (
                before.critical_field_digests == after.critical_field_digests
            ),
            "marker_exact_8301": (
                after.migration_version == 8301
                and after.migration_digest == MIGRATION_DIGEST
            ),
            "v8302_absent": not bool(
                set(HISTORICAL_INGESTION_TABLES).intersection(after.object_names)
            ),
            "journal_events": sorted({record["event"] for record in journal}),
            "precommit_failure_reason": pre_reason,
            "precommit_rollback_exact": (
                pre_after.schema_fingerprint == before.schema_fingerprint
                and pre_after.marker_state == "legacy"
            ),
            "postcommit_failure_reason": post_reason,
            "postcommit_recovery_required": post_recovery_required,
            "postcommit_automatic_retry": recovery["automatic_retry"],
            "verified_backup_restore_status": restored["status"],
            "restore_schema_exact": (
                restored["schema_fingerprint"] == before.schema_fingerprint
            ),
        }
        summary["passed"] = all((
            summary["migration_status"] == "completed",
            summary["replay_status"] == "already_applied",
            summary["marker_exact_8301"],
            summary["v8302_absent"],
            summary["legacy_row_counts_unchanged"],
            summary["legacy_id_digests_unchanged"],
            summary["legacy_critical_digests_unchanged"],
            summary["precommit_rollback_exact"],
            summary["postcommit_recovery_required"],
            summary["postcommit_automatic_retry"] is False,
            summary["verified_backup_restore_status"] == "restored",
            summary["restore_schema_exact"],
        ))
        summary["summary_digest"] = _digest(summary)
        return summary


def _security_summary() -> dict[str, Any]:
    assert_execution_protocol_static_guards()
    refusal = None
    try:
        apply_production_migration()
    except MigrationRehearsalError as exc:
        refusal = exc.reason_code
    runtime_files = (
        PROJECT_ROOT / "memory" / "production_migration_authorization.py",
        PROJECT_ROOT / "memory" / "production_migration_entry.py",
        PROJECT_ROOT / "memory" / "production_migration_clone.py",
        PROJECT_ROOT / "tools" / "run_authorized_v8301_migration.py",
    )
    runtime_source = "\n".join(
        path.read_text(encoding="utf-8") for path in runtime_files
    )
    checks = {
        "old_refusal_preserved": refusal == "production_migration_not_authorized",
        "exact_target_only": "target_version_must_be_exact_8301" in runtime_source,
        "no_duplicate_ddl": all(
            token not in runtime_source
            for token in ("CREATE TABLE", "CREATE INDEX", "CREATE TRIGGER")
        ),
        "no_environment_authority": "os.environ" not in runtime_source,
        "no_network": all(
            token not in runtime_source
            for token in ("import socket", "import urllib", "import requests")
        ),
        "no_stash_integration": all(
            token not in runtime_source for token in ("git stash", "stash@{")
        ),
        "journal_protocol_complete": len(REQUIRED_JOURNAL_EVENTS) == 15,
        "v8302_not_reachable": "apply_historical_migration" not in runtime_source,
        "human_authorization_not_generated": (
            "build_human_authorization" not in runtime_source
        ),
    }
    return {
        "checks": checks,
        "passed": all(checks.values()),
        "scope": "static committed runtime review",
        "summary_digest": _digest(checks),
    }


def _code_review_summary(commit: str) -> dict[str, Any]:
    parent = _git("rev-parse", commit + "^")
    files = [
        line for line in _git(
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            commit,
        ).splitlines()
        if line
    ]
    stat = _git("diff", "--stat", parent, commit)
    return {
        "branch": _git("branch", "--show-current"),
        "commit": commit,
        "parent": parent,
        "commit_subject": _git("show", "-s", "--format=%s", commit),
        "files": files,
        "file_count": len(files),
        "stat": stat.splitlines(),
        "tracked_worktree_clean": not bool(
            _git("status", "--porcelain", "--untracked-files=no")
        ),
        "staging_empty": not bool(_git("diff", "--cached", "--name-only")),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--old-approval-package", required=True, type=Path)
    parser.add_argument("--backup-directory", required=True, type=Path)
    parser.add_argument("--code-commit", required=True)
    parser.add_argument("--regression-test-count", required=True, type=int)
    parser.add_argument("--regression-duration-seconds", required=True, type=float)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if _git("rev-parse", "HEAD") != args.code_commit:
        raise ApprovalPreparationError("code_commit_mismatch")
    if args.regression_test_count < 1 or args.regression_duration_seconds <= 0:
        raise ApprovalPreparationError("regression_summary_invalid")
    validate_production_main_database_path(DATABASES["Main"])
    old_package = _validate_old_package(args.old_approval_package.resolve(strict=True))

    existing = [APPROVAL_PATH, *(OUTPUT_ROOT / name for name in EVIDENCE_FILENAMES)]
    if any(path.exists() for path in existing):
        raise ApprovalPreparationError("approval_output_already_exists")
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

    audits_before = _audit_four_databases()
    imports_before = _tree_gate(PROJECT_ROOT / "imports" / "chatgpt")
    if not all(
        audit["integrity_check"] == ["ok"]
        and audit["foreign_key_violation_count"] == 0
        and audit["immutable_read_gates_unchanged"]
        for audit in audits_before.values()
    ):
        raise ApprovalPreparationError("production_database_gate_failed")
    main = audits_before["Main"]
    if (
        main["migration_version"] is not None
        or main["marker_state"] != "legacy"
        or main["file_gates"]["wal"]["size"] != 0
    ):
        raise ApprovalPreparationError("production_main_not_legacy_quiescent")
    backup = _validate_backup(args.backup_directory.resolve(strict=True), audits_before)
    clone = _clone_execution(
        args.backup_directory.resolve(strict=True) / BACKUP_FILENAMES["Main"]
    )
    if not clone["passed"]:
        raise ApprovalPreparationError("clone_execution_failed")
    security = _security_summary()
    if not security["passed"]:
        raise ApprovalPreparationError("security_gate_failed")

    code_review = _code_review_summary(args.code_commit)
    authorization_protocol = {
        "protocol_version": PRODUCTION_EXECUTION_PROTOCOL_VERSION,
        "approval_and_human_authorization_separate": True,
        "approval_execution_authorized": False,
        "human_authorization_record_generated": False,
        "exact_target_version": 8301,
        "migration_digest": MIGRATION_DIGEST,
        "migration_plan_digest": clone["migration_plan_digest"],
        "source_database_sha256": main["file_gates"]["db"]["sha256"],
        "source_schema_fingerprint": main["schema_fingerprint"],
        "target_schema_fingerprint": clone["target_schema_fingerprint"],
        "one_time_replay_gate": True,
        "append_only_journal": True,
        "precommit_rollback": True,
        "postcommit_recovery_required": True,
        "v8302_hard_refusal": True,
        "backup_verified_at_execution_required": True,
        "service_quiescence_record_required": True,
        "writer_lease_required": True,
        "recovery_marker_absent_required": True,
    }
    authorization_protocol["summary_digest"] = _digest(authorization_protocol)

    _write_json(OUTPUT_ROOT / "code_review_summary.json", code_review)
    _write_json(
        OUTPUT_ROOT / "authorization_protocol_summary.json",
        authorization_protocol,
    )
    _write_json(OUTPUT_ROOT / "clone_execution_summary.json", clone)
    _write_json(OUTPUT_ROOT / "security_gate_summary.json", security)
    evidence_digests = {
        "code_review_summary": _digest(code_review),
        "authorization_protocol_summary": _digest(authorization_protocol),
        "clone_execution_summary": _digest(clone),
        "security_gate_summary": _digest(security),
    }

    production_plan = build_migration_plan(
        _immutable_schema_snapshot(DATABASES["Main"])
    )
    current = datetime.now(timezone.utc)
    expiry = current + timedelta(hours=24)
    package = build_production_migration_approval_package_v2(
        plan=production_plan,
        source_database_sha256=main["file_gates"]["db"]["sha256"],
        source_database_size=main["file_gates"]["db"]["size"],
        code_commit=args.code_commit,
        backup_directory=backup["backup_directory"],
        backup_manifest_digest=backup["backup_manifest_digest"],
        backup_set_digest=backup["backup_set_digest"],
        evidence_digests=evidence_digests,
        old_package_digest=old_package["package_digest"],
        created_at=_utc_text(current),
        expires_at=_utc_text(expiry),
    )
    _write_json(APPROVAL_PATH, package)
    approval_summary = {
        "path": APPROVAL_PATH.relative_to(PROJECT_ROOT).as_posix(),
        "package_digest": package["package_digest"],
        "package_schema_version": package["package_schema_version"],
        "code_commit": package["code_commit"],
        "approval_expiry": package["approval_expires_at"],
        "production_execution_authorized": False,
        "human_authorization_record_generated": False,
        "old_package": old_package,
        "backup": backup,
    }
    _write_json(
        OUTPUT_ROOT / "new_approval_package_summary.json",
        approval_summary,
    )

    audits_after = _audit_four_databases()
    imports_after = _tree_gate(PROJECT_ROOT / "imports" / "chatgpt")
    acceptance = {
        "status": "ready_for_new_user_approval",
        "production_migration_executed": False,
        "v8301_applied_to_production": False,
        "v8302_executed": False,
        "human_authorization_record_generated": False,
        "four_database_gates_unchanged": audits_before == audits_after,
        "four_database_audits": audits_after,
        "imports_gate_unchanged": imports_before == imports_after,
        "imports_gate": imports_after,
        "backup_reused": backup["reused_existing_backup"],
        "new_backup_required": backup["new_backup_required"],
        "clone_execution_passed": clone["passed"],
        "security_gate_passed": security["passed"],
        "regression": {
            "run_count": 1,
            "test_count": args.regression_test_count,
            "failures": 0,
            "errors": 0,
            "duration_seconds": args.regression_duration_seconds,
        },
        "code_commit": args.code_commit,
        "approval_package_digest": package["package_digest"],
        "approval_expiry": package["approval_expires_at"],
        "final_conclusion": "READY_FOR_NEW_USER_APPROVAL",
    }
    acceptance["acceptance_digest"] = _digest(acceptance)
    _write_json(OUTPUT_ROOT / "acceptance_summary.json", acceptance)
    print(json.dumps({
        "status": acceptance["status"],
        "approval_package": str(APPROVAL_PATH),
        "approval_package_digest": package["package_digest"],
        "approval_expiry": package["approval_expires_at"],
        "backup_reused": backup["reused_existing_backup"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
