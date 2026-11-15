"""Clone, backup, compatibility, synthetic, and recovery helpers for v8.3-F."""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
from typing import Any, Mapping

from memory.ingestion_core import canonical_json_bytes, utc_now
from memory.ingestion_migrations import (
    INGESTION_TABLES,
    MIGRATION_DIGEST,
    MIGRATION_ID,
    MIGRATION_VERSION,
    MigrationPlan,
    MigrationRehearsalError,
    PRODUCTION_DATABASE,
    SchemaSnapshot,
    build_migration_plan,
    inspect_schema,
    inspect_schema_connection,
    validate_clone_database_path,
)


BACKUP_RESULT_SCHEMA_VERSION = "v8.3-f-consistent-backup-1"
COMPATIBILITY_SCHEMA_VERSION = "v8.3-f-compatibility-1"
SYNTHETIC_RESULT_SCHEMA_VERSION = "v8.3-f-synthetic-integration-1"
ROLLBACK_RESULT_SCHEMA_VERSION = "v8.3-f-rollback-result-1"
FILE_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class FileGate:
    exists: bool
    size: int
    mtime_ns: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "exists": self.exists,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class ConsistentBackupResult:
    source_schema_fingerprint: str
    backup_schema_fingerprint: str
    source_gates_unchanged: bool
    logical_content_equal: bool
    integrity_check: str
    backup_size: int
    result_schema_version: str = BACKUP_RESULT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "result_schema_version": self.result_schema_version,
            "source_schema_fingerprint": self.source_schema_fingerprint,
            "backup_schema_fingerprint": self.backup_schema_fingerprint,
            "source_gates_unchanged": self.source_gates_unchanged,
            "logical_content_equal": self.logical_content_equal,
            "integrity_check": self.integrity_check,
            "backup_size": self.backup_size,
            "wal_or_shm_copied": False,
        }


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(FILE_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def capture_file_gate(path: str | os.PathLike[str]) -> FileGate:
    target = Path(path)
    if not target.exists():
        return FileGate(False, 0, 0, hashlib.sha256(b"").hexdigest())
    stat = target.stat()
    return FileGate(True, stat.st_size, stat.st_mtime_ns, _hash_file(target))


def capture_sqlite_file_gates(path: str | os.PathLike[str]) -> dict[str, FileGate]:
    target = Path(path)
    return {
        "db": capture_file_gate(target),
        "wal": capture_file_gate(Path(str(target) + "-wal")),
        "shm": capture_file_gate(Path(str(target) + "-shm")),
    }


def _legacy_equal(left: SchemaSnapshot, right: SchemaSnapshot) -> bool:
    return (
        dict(left.row_counts) == dict(right.row_counts)
        and dict(left.id_set_digests) == dict(right.id_set_digests)
        and dict(left.critical_field_digests) == dict(right.critical_field_digests)
    )


def _inspect_immutable(path: Path) -> SchemaSnapshot:
    with closing(
        sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
    ) as conn:
        conn.execute("PRAGMA query_only=ON")
        return inspect_schema_connection(conn)


def create_consistent_backup(
    source_path: str | os.PathLike[str],
    backup_path: str | os.PathLike[str],
) -> ConsistentBackupResult:
    """Use SQLite online backup from a read-only source into a new clone file."""
    source = Path(source_path).resolve(strict=True)
    if not source.is_file():
        raise MigrationRehearsalError("backup_source_not_file")
    target = validate_clone_database_path(backup_path, must_exist=False)
    if target.exists():
        raise MigrationRehearsalError("backup_target_exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    before_gates = capture_sqlite_file_gates(source)
    before = _inspect_immutable(source)
    if before_gates["wal"].exists and before_gates["wal"].size != 0:
        raise MigrationRehearsalError("backup_source_wal_not_quiescent")
    try:
        # The rehearsed production baseline has a zero-byte WAL.  immutable=1
        # is therefore complete and, unlike a normal read-only WAL connection,
        # does not touch the source SHM metadata.
        source_conn = sqlite3.connect(
            source.as_uri() + "?mode=ro&immutable=1", uri=True,
        )
        source_conn.execute("PRAGMA query_only=ON")
        target_conn = sqlite3.connect(target)
        source_conn.backup(target_conn)
        target_conn.close()
        source_conn.close()
    except (sqlite3.Error, OSError) as exc:
        if target.exists():
            target.unlink()
        raise MigrationRehearsalError("backup_creation_failed") from exc
    after_gates = capture_sqlite_file_gates(source)
    after = _inspect_immutable(source)
    backup = inspect_schema(target)
    with closing(sqlite3.connect(target.as_uri() + "?mode=ro&immutable=1", uri=True)) as conn:
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    gates_unchanged = before_gates == after_gates
    logical_equal = _legacy_equal(before, backup) and _legacy_equal(before, after)
    if not gates_unchanged:
        raise MigrationRehearsalError("backup_source_gate_changed")
    if not logical_equal or integrity != "ok":
        raise MigrationRehearsalError("backup_verification_failed")
    return ConsistentBackupResult(
        source_schema_fingerprint=before.schema_fingerprint,
        backup_schema_fingerprint=backup.schema_fingerprint,
        source_gates_unchanged=True,
        logical_content_equal=True,
        integrity_check=integrity,
        backup_size=target.stat().st_size,
    )


def _digest_query(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> str:
    digest = hashlib.sha256()
    for row in conn.execute(sql, params):
        digest.update(canonical_json_bytes(list(row)))
        digest.update(b"\n")
    return digest.hexdigest()


def capture_legacy_compatibility(
    db_path: str | os.PathLike[str],
) -> dict[str, Any]:
    """Capture contract digests only; no private row value is returned."""
    target = Path(db_path).resolve(strict=True)
    snapshot = inspect_schema(target)
    suffix = "?mode=ro&immutable=1" if target == PRODUCTION_DATABASE else "?mode=ro"
    conn = sqlite3.connect(target.as_uri() + suffix, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    try:
        conversation_columns = set(snapshot.table_columns.get("conversations", ()))
        message_columns = set(snapshot.table_columns.get("messages", ()))
        source_columns = {
            "conversations.source_platform": "source_platform" in conversation_columns,
            "conversations.source_id": "source_id" in conversation_columns,
            "messages.source_platform": "source_platform" in message_columns,
            "messages.source_id": "source_id" in message_columns,
        }
        desktop_digest = _digest_query(
            conn,
            """
            SELECT conversation_id,title,update_time_iso,message_count,source_platform
            FROM conversations
            ORDER BY update_time DESC,conversation_id
            LIMIT 20
            """,
        )
        fts_digest = _digest_query(
            conn,
            """
            SELECT m.rowid,m.conversation_id,m.message_id
            FROM messages_fts
            JOIN messages m ON m.rowid=messages_fts.rowid
            ORDER BY m.rowid
            LIMIT 20
            """,
        )
        fts_count = int(conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0])
        focus = conn.execute(
            """
            SELECT conversation_id,message_id,rowid
            FROM messages
            WHERE conversation_id IS NOT NULL AND message_id IS NOT NULL
            GROUP BY conversation_id,message_id
            HAVING COUNT(*)=1
            ORDER BY rowid
            LIMIT 1
            """
        ).fetchone()
        message_context_digest = _digest({"status": "no_fixture"})
        conversation_reader_digest = _digest({"status": "no_fixture"})
        if focus is not None:
            context_rows = conn.execute(
                """
                WITH ordered AS (
                    SELECT rowid,conversation_id,message_id,role,create_time_iso,text,
                           ROW_NUMBER() OVER (
                             ORDER BY ordinal IS NULL,ordinal,
                                      create_time IS NULL,create_time,rowid
                           ) position
                    FROM messages WHERE conversation_id=?
                ), focus_position AS (
                    SELECT position FROM ordered WHERE rowid=?
                )
                SELECT ordered.conversation_id,ordered.message_id,ordered.role,
                       ordered.create_time_iso,ordered.text,ordered.position
                FROM ordered,focus_position
                WHERE ordered.position BETWEEN focus_position.position-3
                                           AND focus_position.position+3
                ORDER BY ordered.position
                """,
                (focus["conversation_id"], focus["rowid"]),
            ).fetchall()
            message_context_digest = _digest([list(row) for row in context_rows])
            reader_conversation = conn.execute(
                """
                SELECT conversation_id,title,create_time_iso,update_time_iso,
                       message_count,default_model_slug,source_platform,memory_space
                FROM conversations WHERE conversation_id=?
                """,
                (focus["conversation_id"],),
            ).fetchone()
            reader_rows = conn.execute(
                """
                SELECT message_id,node_id,conversation_id,ordinal,parent_id,role,
                       author_name,create_time,create_time_iso,update_time_iso,
                       content_type,text,metadata_json,source_platform,memory_space
                FROM messages
                WHERE conversation_id=?
                ORDER BY ordinal IS NULL,ordinal,
                         create_time IS NULL,create_time,rowid
                LIMIT 20
                """,
                (focus["conversation_id"],),
            ).fetchall()
            conversation_reader_digest = _digest(
                {
                    "conversation": list(reader_conversation),
                    "messages": [list(row) for row in reader_rows],
                    "anchor_message_id": focus["message_id"],
                }
            )
        attachment_digest = _digest_query(
            conn,
            "SELECT message_id,metadata_json FROM messages ORDER BY rowid",
        )
        source_digest = _digest_query(
            conn,
            "SELECT conversation_id,source_platform FROM conversations ORDER BY rowid",
        )
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = len(list(conn.execute("PRAGMA foreign_key_check")))
        new_table_counts = {
            table: int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in INGESTION_TABLES
            if table in snapshot.table_columns
        }
        return {
            "compatibility_schema_version": COMPATIBILITY_SCHEMA_VERSION,
            "schema_fingerprint": snapshot.schema_fingerprint,
            "row_counts": dict(snapshot.row_counts),
            "id_set_digests": dict(snapshot.id_set_digests),
            "critical_field_digests": dict(snapshot.critical_field_digests),
            "source_column_presence": source_columns,
            "source_values_digest": source_digest,
            "attachment_json_digest": attachment_digest,
            "desktop_query_digest": desktop_digest,
            "fts_contract_digest": fts_digest,
            "fts_row_count": fts_count,
            "message_context_digest": message_context_digest,
            "conversation_messages_digest": conversation_reader_digest,
            "readonly_open_passed": True,
            "integrity_check": integrity,
            "foreign_key_violation_count": foreign_keys,
            "new_table_row_counts": new_table_counts,
        }
    finally:
        conn.close()


def run_compatibility_suite(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    require_empty_ingestion_tables: bool = True,
) -> dict[str, Any]:
    exact_fields = (
        "row_counts",
        "id_set_digests",
        "critical_field_digests",
        "source_column_presence",
        "source_values_digest",
        "attachment_json_digest",
        "desktop_query_digest",
        "fts_contract_digest",
        "fts_row_count",
        "message_context_digest",
        "conversation_messages_digest",
    )
    checks = {field: before.get(field) == after.get(field) for field in exact_fields}
    checks["readonly_open"] = after.get("readonly_open_passed") is True
    checks["integrity"] = after.get("integrity_check") == "ok"
    checks["foreign_keys"] = after.get("foreign_key_violation_count") == 0
    if require_empty_ingestion_tables:
        counts = after.get("new_table_row_counts")
        checks["new_ingestion_tables_empty"] = (
            isinstance(counts, Mapping)
            and set(counts) == set(INGESTION_TABLES)
            and all(value == 0 for value in counts.values())
        )
    passed = all(checks.values())
    return {
        "compatibility_schema_version": COMPATIBILITY_SCHEMA_VERSION,
        "passed": passed,
        "checks": checks,
        "before_contract_digest": _digest(dict(before)),
        "after_contract_digest": _digest(dict(after)),
    }


def _canonical_json_text(value: Any) -> str:
    raw = canonical_json_bytes(value).decode("utf-8")
    if canonical_json_bytes(json.loads(raw)).decode("utf-8") != raw:
        raise MigrationRehearsalError("noncanonical_json")
    return raw


def _safe_digest(label: str) -> str:
    return hashlib.sha256(("v8.3-f-synthetic:" + label).encode("utf-8")).hexdigest()


def persist_synthetic_integration(
    clone_path: str | os.PathLike[str],
    *,
    external_contract_digests: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Persist one fully synthetic A-E audit chain into the new tables only."""
    target = validate_clone_database_path(clone_path)
    before = capture_legacy_compatibility(target)
    now = "2026-07-28T12:00:00.000000Z"
    expires = "2026-07-29T12:00:00.000000Z"
    manifest_id = "manifest_" + _safe_digest("manifest")[:32]
    batch_id = "batch_" + _safe_digest("batch")[:32]
    conflict_id = "conflict_" + _safe_digest("conflict")[:32]
    decision_id = "decision_" + _safe_digest("decision")[:32]
    approval_id = "approval_" + _safe_digest("approval")[:32]
    execution_id = "execution_" + _safe_digest("execution")[:32]
    verification_id = "verification_" + _safe_digest("verification")[:32]
    entity_digest = _safe_digest("entity")
    approval_digest = _safe_digest("approval-digest")
    verification_digest = _safe_digest("verification-digest")
    plan_digest = _safe_digest("plan")
    stage_digest = _safe_digest("stage")
    snapshot_digest = _safe_digest("target")
    asset_digest = _safe_digest("asset")
    external = dict(external_contract_digests or {})
    for key, value in external.items():
        if not isinstance(key, str) or not isinstance(value, str) or len(value) != 64:
            raise MigrationRehearsalError("invalid_external_contract_digest")
    conn = sqlite3.connect(target, isolation_level=None)
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO ingestion_source_manifests VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                manifest_id, "chatgpt", "synthetic", "unified_chatgpt",
                "v8.3-c", _safe_digest("archive"), 128, "zip",
                "v8.3-a-source-manifest-1", "valid",
                _canonical_json_text({"conversations": 1, "messages": 1}),
                _canonical_json_text({"entities": 1, "occurrences": 1}),
                _canonical_json_text([]), now, now,
            ),
        )
        conn.execute(
            "INSERT INTO ingestion_batches VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                batch_id, manifest_id, "full", "completed", plan_digest,
                approval_digest, snapshot_digest, stage_digest, "not_required",
                0, 0, 1, "v8.3-a-import-batch-1", now, now,
            ),
        )
        event_payload = _safe_digest("batch-event-payload")
        event_digest = _safe_digest("batch-event")
        conn.execute(
            "INSERT INTO ingestion_batch_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                batch_id, 1, "event_" + event_digest, "completed", "verifying",
                "completed", now, "test_reviewer", "synthetic_completed",
                event_payload, "0" * 64, event_digest, "v8.3-a-batch-event-1",
            ),
        )
        conn.execute(
            "INSERT INTO ingestion_source_bindings VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "chatgpt", "conversation", "synthetic-source-1",
                "conversation", "synthetic-target-1", manifest_id, batch_id,
                _safe_digest("source"), "active", "v8.3-f-source-binding-1", now,
            ),
        )
        conn.execute(
            "INSERT INTO ingestion_asset_entities VALUES(?,?,?,?,?,?,?,?,?)",
            (
                entity_digest, "image/png", 68, "verified",
                "entities/" + entity_digest[:2] + "/" + entity_digest,
                _safe_digest("entity-verification"), batch_id,
                "v8.3-f-asset-entity-1", now,
            ),
        )
        conn.execute(
            "INSERT INTO ingestion_attachment_bindings VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "media_" + _safe_digest("media")[:32],
                _safe_digest("source-attachment"), "synthetic-message-1",
                entity_digest, manifest_id, batch_id, 0, "active",
                "v8.3-f-attachment-binding-1", now,
            ),
        )
        conn.execute(
            "INSERT INTO ingestion_conflicts VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                conflict_id, batch_id, "metadata_only", "medium",
                _safe_digest("conflict-evidence"),
                _canonical_json_text(["keep_separate"]), "keep_separate", 1,
                "decided", "v8.3-d-conflict-1", now,
            ),
        )
        conn.execute(
            "INSERT INTO ingestion_decisions VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                decision_id, conflict_id, "keep_separate",
                _canonical_json_text(["operation_synthetic_approved"]),
                _canonical_json_text([]), _safe_digest("decision-evidence"),
                "synthetic_reviewed", "test_reviewer", "v8.3-e-conflict-decision-1", now,
            ),
        )
        conn.execute(
            "INSERT INTO ingestion_approvals VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                approval_id, batch_id, plan_digest, stage_digest, snapshot_digest,
                approval_digest, "exact_plan", expires, "test_reviewer",
                "v8.3-e-approval-1", now,
            ),
        )
        conn.execute(
            "INSERT INTO ingestion_executions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                execution_id, batch_id, approval_digest, "completed",
                snapshot_digest, _safe_digest("database-after"), asset_digest,
                _safe_digest("asset-after"), verification_digest, "not_required",
                "v8.3-e-execution-result-1", now, now,
            ),
        )
        execution_event_digest = _safe_digest("execution-event")
        conn.execute(
            "INSERT INTO ingestion_execution_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                execution_id, 1, "event_" + execution_event_digest,
                "execution_completed", "verifying", "completed", now,
                "test_executor", "synthetic_completed",
                _safe_digest("execution-event-payload"), "0" * 64,
                execution_event_digest, "v8.3-e-execution-journal-1",
            ),
        )
        invariant_results = {
            "adapter": external.get("adapter", _safe_digest("adapter")),
            "planner": external.get("planner", _safe_digest("planner")),
            "staging": external.get("staging", _safe_digest("staging")),
            "approval": external.get("approval", approval_digest),
            "execution": external.get("execution", _safe_digest("execution-contract")),
            "verification": external.get("verification", verification_digest),
        }
        conn.execute(
            "INSERT INTO ingestion_verifications VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                verification_id, execution_id, 1,
                _canonical_json_text({"synthetic_records": 1}),
                _canonical_json_text({"synthetic_records": 1}),
                _canonical_json_text(invariant_results),
                _safe_digest("database-after"), _safe_digest("asset-after"),
                verification_digest, "v8.3-e-verification-1", now,
            ),
        )
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    after = capture_legacy_compatibility(target)
    compatibility = run_compatibility_suite(
        before, after, require_empty_ingestion_tables=False,
    )
    counts = after["new_table_row_counts"]
    expected_nonempty = set(INGESTION_TABLES) - {"ingestion_leases"}
    records_present = all(counts.get(name) == 1 for name in expected_nonempty)
    passed = bool(compatibility["passed"] and records_present and counts["ingestion_leases"] == 0)
    return {
        "result_schema_version": SYNTHETIC_RESULT_SCHEMA_VERSION,
        "passed": passed,
        "synthetic_only": True,
        "legacy_data_unchanged": compatibility["passed"],
        "state_machine_completed": True,
        "table_record_counts": counts,
        "external_contracts_bound": sorted(external),
        "result_digest": _digest({
            "passed": passed,
            "counts": counts,
            "external_contracts": external,
        }),
    }


def restore_from_backup(
    clone_path: str | os.PathLike[str],
    backup_path: str | os.PathLike[str],
) -> dict[str, Any]:
    """Replace only a disposable clone using a verified online-backup copy."""
    target = validate_clone_database_path(clone_path)
    backup = validate_clone_database_path(backup_path)
    backup_snapshot = inspect_schema(backup)
    with closing(sqlite3.connect(backup.as_uri() + "?mode=ro&immutable=1", uri=True)) as conn:
        if str(conn.execute("PRAGMA integrity_check").fetchone()[0]) != "ok":
            raise MigrationRehearsalError("backup_corrupt")
    temporary = target.with_name(target.name + ".restore.tmp")
    if temporary.exists():
        raise MigrationRehearsalError("restore_temporary_exists")
    try:
        source_conn = sqlite3.connect(backup.as_uri() + "?mode=ro&immutable=1", uri=True)
        destination_conn = sqlite3.connect(temporary)
        source_conn.backup(destination_conn)
        destination_conn.close()
        source_conn.close()
        restored_snapshot = inspect_schema(temporary)
        if not _legacy_equal(backup_snapshot, restored_snapshot):
            raise MigrationRehearsalError("restore_verification_failed")
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(str(target) + suffix)
            if sidecar.exists():
                sidecar.unlink()
        os.replace(temporary, target)
        for suffix in ("-wal", "-shm", "-journal"):
            temporary_sidecar = Path(str(temporary) + suffix)
            if temporary_sidecar.exists():
                temporary_sidecar.unlink()
    except (sqlite3.Error, OSError) as exc:
        if temporary.exists():
            temporary.unlink()
        raise MigrationRehearsalError("restore_failed") from exc
    final_snapshot = inspect_schema(target)
    if final_snapshot.schema_fingerprint != backup_snapshot.schema_fingerprint:
        raise MigrationRehearsalError("restore_schema_mismatch")
    return {
        "result_schema_version": ROLLBACK_RESULT_SCHEMA_VERSION,
        "status": "restored",
        "schema_fingerprint": final_snapshot.schema_fingerprint,
        "legacy_logical_content_equal": _legacy_equal(backup_snapshot, final_snapshot),
        "manual_service_stop_required_for_production": True,
    }


def assess_recovery_state(
    clone_path: str | os.PathLike[str],
    *,
    plan: MigrationPlan | None = None,
) -> dict[str, Any]:
    snapshot = inspect_schema(validate_clone_database_path(clone_path))
    expected_plan = plan
    if expected_plan is None and snapshot.marker_state == "legacy":
        expected_plan = build_migration_plan(snapshot)
    if snapshot.marker_state == "legacy":
        status = "not_applied"
        action = "retry_only_after_operator_review"
    elif (
        snapshot.marker_state == "marked"
        and snapshot.migration_version == MIGRATION_VERSION
        and snapshot.migration_digest == MIGRATION_DIGEST
        and expected_plan is not None
        and snapshot.schema_fingerprint == expected_plan.expected_target_schema_fingerprint
    ):
        status = "completed"
        action = "no_recovery_required"
    else:
        status = "recovery_required"
        action = "stop_and_restore_verified_backup"
    return {
        "status": status,
        "manual_action": action,
        "automatic_retry": False,
        "schema_fingerprint": snapshot.schema_fingerprint,
        "marker_state": snapshot.marker_state,
    }


def approval_expiry(hours: int = 24) -> str:
    if hours < 1 or hours > 168:
        raise MigrationRehearsalError("invalid_approval_expiry")
    return (
        datetime.now(timezone.utc) + timedelta(hours=hours)
    ).isoformat(timespec="seconds").replace("+00:00", "Z")


def production_source_is_readonly(path: str | os.PathLike[str]) -> bool:
    return Path(path).resolve(strict=True) == PRODUCTION_DATABASE
