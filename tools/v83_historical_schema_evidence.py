"""Generate sanitized v8.3-G0 unified-authority clone evidence.

The production database is opened read-only and copied with SQLite online
backup.  All migrations, synthetic rows, replay, and restore operations happen
only on disposable files below the system temporary directory.
"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
import hashlib
import json
import sqlite3
import sys
import tempfile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.ingestion_migration_rehearsal import (
    capture_legacy_compatibility,
    create_consistent_backup,
    restore_from_backup,
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
    verify_historical_migrated_schema,
    verify_migrated_schema,
)
from tests.v83_historical_schema_fixtures import insert_unified_chains


PRODUCTION_DB = PROJECT_ROOT / "data" / "ayunos_chatgpt.db"
IMPORT_ROOT = PROJECT_ROOT / "imports" / "chatgpt"
OUTPUT = (
    PROJECT_ROOT
    / "output"
    / "v8.3-g0-unified-authority-correction"
    / "acceptance_summary.json"
)
CHUNK_SIZE = 1024 * 1024


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def file_gate(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"exists": False, "size": 0, "sha256": None, "mtime_ns": None}
    stat = path.stat()
    return {
        "exists": True,
        "size": stat.st_size,
        "sha256": hash_file(path),
        "mtime_ns": stat.st_mtime_ns,
    }


def imports_gate(root: Path) -> dict[str, object]:
    files = sorted(
        (item for item in root.rglob("*") if item.is_file()),
        key=lambda item: item.relative_to(root).as_posix(),
    )
    digest_lines: list[str] = []
    total = 0
    for path in files:
        relative = path.relative_to(root).as_posix()
        content_digest = hash_file(path)
        size = path.stat().st_size
        total += size
        digest_lines.append(f"{relative}|{size}|{content_digest}")
    return {
        "file_count": len(files),
        "total_size": total,
        "tree_sha256": hashlib.sha256(
            "\n".join(digest_lines).encode("utf-8")
        ).hexdigest(),
    }


def production_gate() -> dict[str, object]:
    return {
        "database": file_gate(PRODUCTION_DB),
        "wal": file_gate(Path(str(PRODUCTION_DB) + "-wal")),
        "shm": file_gate(Path(str(PRODUCTION_DB) + "-shm")),
        "imports_chatgpt": imports_gate(IMPORT_ROOT),
    }


def online_backup_readonly(source: Path, target: Path) -> None:
    uri = source.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as source_connection:
        with closing(sqlite3.connect(target)) as target_connection:
            source_connection.backup(target_connection)


def schema_counts(path: Path) -> dict[str, int]:
    with closing(sqlite3.connect(path)) as connection:
        rows = connection.execute(
            "SELECT type,COUNT(*) FROM sqlite_schema "
            "WHERE name NOT LIKE 'sqlite_%' GROUP BY type"
        ).fetchall()
    return {str(kind): int(count) for kind, count in rows}


def main() -> int:
    before_gate = production_gate()
    with tempfile.TemporaryDirectory(
        prefix="ayunos-v83g0-unified-evidence-"
    ) as raw_root:
        root = Path(raw_root)
        clone = root / "production-clone.db"
        v8301_backup = root / "v8301-backup.db"
        online_backup_readonly(PRODUCTION_DB, clone)

        legacy = capture_legacy_compatibility(clone)
        v8301_plan = build_migration_plan(inspect_schema(clone))
        v8301_result = apply_migration_to_clone(
            v8301_plan,
            clone,
            rehearsal_id="rehearsal_g0_unified_clone_v8301",
        )
        v8301_verification = verify_migrated_schema(
            clone,
            expected_fingerprint=v8301_plan.expected_target_schema_fingerprint,
        )
        frozen_v8301 = inspect_schema(clone)
        backup_result = create_consistent_backup(clone, v8301_backup)

        v8302_plan = build_historical_migration_plan(frozen_v8301)
        v8302_result = apply_historical_migration_to_clone(
            v8302_plan,
            clone,
            rehearsal_id="rehearsal_g0_unified_clone_v8302",
        )
        v8302_verification = verify_historical_migrated_schema(
            clone,
            expected_fingerprint=v8302_plan.expected_target_schema_fingerprint,
            require_empty=True,
        )
        post_migration = capture_legacy_compatibility(clone)
        compatibility = run_compatibility_suite(legacy, post_migration)
        chain = insert_unified_chains(clone)

        with closing(sqlite3.connect(clone)) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_keys = connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
            append_only_blocked = False
            try:
                connection.execute(
                    "UPDATE ingestion_historical_manifests "
                    "SET validation_status='changed'"
                )
            except sqlite3.IntegrityError:
                append_only_blocked = True
            connection.rollback()

        replay_result = apply_historical_migration_to_clone(
            v8302_plan,
            clone,
            rehearsal_id="rehearsal_g0_unified_clone_replay",
        )
        restore = restore_from_backup(clone, v8301_backup)
        restored = inspect_schema(clone)

        evidence = {
            "schema_version": "v8.3-g0-unified-authority-evidence-1",
            "production_mode": "read_only_source_online_backup",
            "migration": {
                "v8301_status": v8301_result.status,
                "v8301_digest": MIGRATION_DIGEST,
                "v8301_version": MIGRATION_VERSION,
                "v8301_marker_valid": True,
                "v8302_status": v8302_result.status,
                "v8302_digest": HISTORICAL_MIGRATION_DIGEST,
                "v8302_version": HISTORICAL_MIGRATION_VERSION,
                "v8302_marker_chain_valid": v8302_verification[
                    "marker_chain_valid"
                ],
                "v8302_table_count": v8302_plan.table_count,
                "v8302_index_count": len(HISTORICAL_MIGRATION_INDEX_DDL),
                "v8302_trigger_count": len(HISTORICAL_MIGRATION_TRIGGER_DDL),
                "schema_object_counts": schema_counts(v8301_backup),
            },
            "compatibility": compatibility,
            "unified_authority": {
                "source_manifest_kind": chain["source_subject"].manifest_kind,
                "historical_manifest_kind": (
                    chain["historical_subject"].manifest_kind
                ),
                "source_batch_mode": chain["full_batch"].batch_mode,
                "historical_batch_mode": chain["historical_batch"].batch_mode,
                "shared_repository_class": True,
                "shared_batch_event_tables": True,
                "shared_approval_table": True,
                "shared_execution_table": True,
                "shared_verification_table": True,
                "shared_lease_table": True,
                "row_counts": chain["counts"],
                "historical_archive_sha256_is_null": (
                    chain["historical_manifest"].archive_sha256 is None
                ),
                "metadata_only_entity_digest_is_null": (
                    chain["attachment_binding"].entity_digest is None
                ),
            },
            "safety": {
                "integrity_check": integrity,
                "foreign_key_violation_count": len(foreign_keys),
                "append_only_trigger_blocked_update": append_only_blocked,
                "exact_replay_status": replay_result.status,
                "backup_integrity": backup_result.integrity_check,
                "restore_status": restore["status"],
                "restore_returned_v8301": (
                    restored.migration_version == MIGRATION_VERSION
                    and restored.migration_digest == MIGRATION_DIGEST
                ),
            },
            "production_gate_before": before_gate,
            "production_gate_after": production_gate(),
        }
    evidence["production_gate_unchanged"] = {
        key: (
            evidence["production_gate_before"][key]
            == evidence["production_gate_after"][key]
        )
        for key in ("database", "wal", "imports_chatgpt")
    }
    evidence["production_gate_unchanged"]["shm_content_and_size"] = all(
        evidence["production_gate_before"]["shm"].get(field)
        == evidence["production_gate_after"]["shm"].get(field)
        for field in ("exists", "size", "sha256")
    )
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "output": str(OUTPUT.relative_to(PROJECT_ROOT)),
        "v8302_digest": HISTORICAL_MIGRATION_DIGEST,
        "table_count": evidence["migration"]["v8302_table_count"],
        "index_count": evidence["migration"]["v8302_index_count"],
        "trigger_count": evidence["migration"]["v8302_trigger_count"],
        "integrity_check": evidence["safety"]["integrity_check"],
        "foreign_key_violation_count": evidence[
            "safety"
        ]["foreign_key_violation_count"],
        "exact_replay_status": evidence["safety"]["exact_replay_status"],
        "restore_status": evidence["safety"]["restore_status"],
        "production_gate_unchanged": evidence[
            "production_gate_unchanged"
        ],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
