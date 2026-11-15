"""Additive, clone-only SQLite migration core for YunOS v8.3-F.

The production database is an inspection source only.  Every writable entry
point rejects the known production database and accepts only disposable
databases under the system temporary directory or the v8.3-F evidence root.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
from typing import Any, Callable, Iterable, Mapping, Sequence

from memory.db_path_policy import DEFAULT_REAL_DB_PATH, is_reparse_point
from memory.ingestion_core import canonical_json_bytes, sanitize_reason_code, utc_now


MIGRATION_ID = "migration_yunos_v8_3_ingestion_core_001"
MIGRATION_VERSION = 8301
MIGRATION_NAME = "yunos_v8_3_ingestion_core"
MIGRATION_SCHEMA_VERSION = "v8.3-f-migration-marker-1"
APPLICATION_VERSION = "YunOS-v8.3-F"
PLAN_SCHEMA_VERSION = "v8.3-f-migration-plan-1"
SNAPSHOT_SCHEMA_VERSION = "v8.3-f-schema-snapshot-1"
RESULT_SCHEMA_VERSION = "v8.3-f-migration-result-1"
APPROVAL_PACKAGE_SCHEMA_VERSION = "v8.3-f-production-migration-approval-1"

NODE_OUTPUT_NAME = "v8.3-f-migration-recovery-rehearsal"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
NODE_OUTPUT_ROOT = (PROJECT_ROOT / "output" / NODE_OUTPUT_NAME).resolve()
SYSTEM_TEMP_ROOT = Path(tempfile.gettempdir()).resolve()
PRODUCTION_DATABASE = DEFAULT_REAL_DB_PATH.resolve()
ZERO_DIGEST = "0" * 64

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_IDENTITY_RE = re.compile(r"[a-z][a-z0-9_.:-]{7,191}\Z")
_TOKEN_RE = re.compile(r"[a-z][a-z0-9_.-]{0,95}\Z")

LEGACY_TABLES = (
    "assets",
    "conversation_marks",
    "conversation_tags",
    "conversations",
    "memory_summaries",
    "message_marks",
    "messages",
    "messages_fts",
)

INGESTION_TABLES = (
    "ingestion_source_manifests",
    "ingestion_batches",
    "ingestion_batch_events",
    "ingestion_source_bindings",
    "ingestion_asset_entities",
    "ingestion_attachment_bindings",
    "ingestion_conflicts",
    "ingestion_decisions",
    "ingestion_approvals",
    "ingestion_executions",
    "ingestion_execution_events",
    "ingestion_verifications",
    "ingestion_leases",
)

MARKER_TABLES = ("schema_migrations", "yunos_schema_meta")
REQUIRED_TABLES = MARKER_TABLES + INGESTION_TABLES


class MigrationRehearsalError(RuntimeError):
    """Fail-closed error carrying only a stable, privacy-safe reason code."""

    def __init__(self, reason_code: str, *, recovery_required: bool = False) -> None:
        reason = sanitize_reason_code(reason_code)
        super().__init__(reason)
        self.reason_code = reason
        self.recovery_required = bool(recovery_required)


class MigrationInterrupted(BaseException):
    """Fault-injection signal used to model an abrupt migrator crash."""

    def __init__(self, checkpoint: str) -> None:
        super().__init__(checkpoint)
        self.checkpoint = checkpoint


@dataclass(frozen=True)
class SchemaSnapshot:
    schema_fingerprint: str
    object_names: tuple[str, ...]
    object_counts: Mapping[str, int]
    object_sql: tuple[tuple[str, str, str, str], ...]
    table_columns: Mapping[str, tuple[str, ...]]
    row_counts: Mapping[str, int]
    id_set_digests: Mapping[str, str]
    critical_field_digests: Mapping[str, str]
    pragmas: Mapping[str, Any]
    marker_state: str
    migration_version: int | None
    migration_digest: str | None
    marker_resulting_fingerprint: str | None
    snapshot_schema_version: str = SNAPSHOT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_schema_version": self.snapshot_schema_version,
            "schema_fingerprint": self.schema_fingerprint,
            "object_names": list(self.object_names),
            "object_counts": dict(self.object_counts),
            "table_columns": {
                name: list(columns) for name, columns in self.table_columns.items()
            },
            "row_counts": dict(self.row_counts),
            "id_set_digests": dict(self.id_set_digests),
            "critical_field_digests": dict(self.critical_field_digests),
            "pragmas": dict(self.pragmas),
            "marker_state": self.marker_state,
            "migration_version": self.migration_version,
            "migration_digest": self.migration_digest,
            "marker_resulting_fingerprint": self.marker_resulting_fingerprint,
        }


@dataclass(frozen=True)
class MigrationPlan:
    migration_id: str
    migration_version: int
    migration_name: str
    migration_digest: str
    source_schema_fingerprint: str
    expected_target_schema_fingerprint: str
    statement_digests: tuple[str, ...]
    table_count: int
    index_count: int
    trigger_count: int
    destructive_statement_count: int
    estimated_size_growth_bytes: int
    plan_schema_version: str = PLAN_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_schema_version": self.plan_schema_version,
            "migration_id": self.migration_id,
            "migration_version": self.migration_version,
            "migration_name": self.migration_name,
            "migration_digest": self.migration_digest,
            "source_schema_fingerprint": self.source_schema_fingerprint,
            "expected_target_schema_fingerprint": self.expected_target_schema_fingerprint,
            "statement_digests": list(self.statement_digests),
            "objects_added": {
                "tables": self.table_count,
                "indexes": self.index_count,
                "triggers": self.trigger_count,
            },
            "destructive_statement_count": self.destructive_statement_count,
            "estimated_size_growth_bytes": self.estimated_size_growth_bytes,
        }


@dataclass(frozen=True)
class MigrationResult:
    status: str
    migration_id: str
    migration_version: int
    migration_digest: str
    before_schema_fingerprint: str
    after_schema_fingerprint: str
    integrity_check: str
    foreign_key_violation_count: int
    recovery_required: bool
    result_schema_version: str = RESULT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "result_schema_version": self.result_schema_version,
            "status": self.status,
            "migration_id": self.migration_id,
            "migration_version": self.migration_version,
            "migration_digest": self.migration_digest,
            "before_schema_fingerprint": self.before_schema_fingerprint,
            "after_schema_fingerprint": self.after_schema_fingerprint,
            "integrity_check": self.integrity_check,
            "foreign_key_violation_count": self.foreign_key_violation_count,
            "recovery_required": self.recovery_required,
        }


def _canonical_text(value: Any) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _digest_rows(rows: Iterable[Sequence[Any]]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(canonical_json_bytes(list(row)))
        digest.update(b"\n")
    return digest.hexdigest()


def _sha(value: str, reason: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise MigrationRehearsalError(reason)
    return value


def _identity(value: str, reason: str) -> str:
    if not isinstance(value, str) or _IDENTITY_RE.fullmatch(value) is None:
        raise MigrationRehearsalError(reason)
    return value


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _reject_reparse_components(path: Path) -> None:
    current = path
    while True:
        if (current.exists() or current.is_symlink()) and is_reparse_point(current):
            raise MigrationRehearsalError("migration_reparse_forbidden")
        if current.parent == current:
            break
        current = current.parent


def validate_clone_database_path(
    value: str | os.PathLike[str], *, must_exist: bool = True,
) -> Path:
    """Resolve an isolated writable DB path and reject production-like targets."""
    raw = os.fspath(value)
    if (
        not isinstance(raw, str)
        or not raw
        or "\x00" in raw
        or raw.startswith(("\\\\", "//"))
        or "\\\\?\\" in raw.lower()
        or "\\\\.\\" in raw.lower()
    ):
        raise MigrationRehearsalError("migration_path_unsafe")
    unresolved = Path(raw)
    if not unresolved.is_absolute():
        raise MigrationRehearsalError("migration_path_not_absolute")
    drive, tail = os.path.splitdrive(raw)
    if ":" in tail or (drive and not re.fullmatch(r"[A-Za-z]:", drive)):
        raise MigrationRehearsalError("migration_path_unsafe")
    if any(part in {"", ".", ".."} or part.endswith((" ", ".")) for part in unresolved.parts[1:]):
        raise MigrationRehearsalError("migration_path_unsafe")
    _reject_reparse_components(unresolved)
    resolved = unresolved.resolve(strict=must_exist)
    if resolved == PRODUCTION_DATABASE:
        raise MigrationRehearsalError("production_migration_not_authorized")
    if not (
        _is_relative_to(resolved, SYSTEM_TEMP_ROOT)
        or _is_relative_to(resolved, NODE_OUTPUT_ROOT)
    ):
        raise MigrationRehearsalError("migration_target_outside_rehearsal_boundary")
    if _is_relative_to(resolved, PROJECT_ROOT / "data") or _is_relative_to(
        resolved, PROJECT_ROOT / "imports"
    ):
        raise MigrationRehearsalError("production_migration_not_authorized")
    if must_exist and not resolved.is_file():
        raise MigrationRehearsalError("migration_target_not_file")
    return resolved


def _normalize_sql(sql: str | None) -> str:
    return " ".join((sql or "").split())


def _schema_objects(conn: sqlite3.Connection) -> tuple[tuple[str, str, str, str], ...]:
    shadow_tables = {
        str(row[1])
        for row in conn.execute("PRAGMA table_list").fetchall()
        if len(row) >= 3 and row[2] == "shadow"
    }
    rows = conn.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_schema "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
    ).fetchall()
    return tuple(
        (str(kind), str(name), str(table_name), _normalize_sql(sql))
        for kind, name, table_name, sql in rows
        if str(name) not in shadow_tables and str(table_name) not in shadow_tables
    )


def schema_fingerprint(conn: sqlite3.Connection) -> str:
    return _digest({"objects": _schema_objects(conn)})


def _table_columns(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(str(row[1]) for row in conn.execute(f'PRAGMA table_xinfo("{table}")'))


def _legacy_row_count(conn: sqlite3.Connection, table: str) -> int:
    row = conn.execute(
        "SELECT 1 FROM sqlite_schema WHERE type IN ('table','view') AND name=?", (table,)
    ).fetchone()
    if row is None:
        return 0
    return int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


def _ordered_column_digest(
    conn: sqlite3.Connection, table: str, columns: Sequence[str],
) -> str:
    actual = set(_table_columns(conn, table))
    selected = [column for column in columns if column in actual]
    if not selected:
        return _digest({"table": table, "columns": []})
    quoted = ",".join(f'"{column}"' for column in selected)
    return _digest_rows(conn.execute(f'SELECT {quoted} FROM "{table}" ORDER BY rowid'))


def inspect_schema_connection(conn: sqlite3.Connection) -> SchemaSnapshot:
    """Inspect schema and privacy-safe logical digests without mutating the DB."""
    objects = _schema_objects(conn)
    object_counts: dict[str, int] = {}
    for kind, _name, _table, _sql in objects:
        object_counts[kind] = object_counts.get(kind, 0) + 1
    table_names = sorted(
        name for kind, name, _table, _sql in objects
        if kind == "table"
    )
    table_columns = {name: _table_columns(conn, name) for name in table_names}
    row_counts = {
        table: _legacy_row_count(conn, table)
        for table in ("conversations", "messages", "assets")
        if table in table_columns
    }
    id_set_digests: dict[str, str] = {}
    if "conversations" in table_columns and "conversation_id" in table_columns["conversations"]:
        id_set_digests["conversation_ids"] = _digest_rows(
            conn.execute(
                "SELECT conversation_id FROM conversations "
                "ORDER BY typeof(conversation_id),conversation_id"
            )
        )
    if "messages" in table_columns and "message_id" in table_columns["messages"]:
        id_set_digests["message_ids"] = _digest_rows(
            conn.execute(
                "SELECT message_id FROM messages "
                "ORDER BY typeof(message_id),message_id,rowid"
            )
        )
    critical_field_digests: dict[str, str] = {}
    if "conversations" in table_columns:
        critical_field_digests["conversations"] = _ordered_column_digest(
            conn,
            "conversations",
            (
                "conversation_id", "title", "create_time", "update_time",
                "source_file", "source_platform", "metadata_json", "memory_space",
            ),
        )
    if "messages" in table_columns:
        critical_field_digests["messages"] = _ordered_column_digest(
            conn,
            "messages",
            (
                "message_id", "node_id", "conversation_id", "parent_id",
                "children_json", "role", "create_time", "update_time",
                "content_type", "text", "metadata_json", "source_platform",
                "memory_space",
            ),
        )
    if "assets" in table_columns:
        critical_field_digests["assets"] = _ordered_column_digest(
            conn, "assets", ("asset_file", "original_name", "size_bytes"),
        )

    marker_state = "legacy"
    migration_version: int | None = None
    migration_digest: str | None = None
    marker_resulting_fingerprint: str | None = None
    if "schema_migrations" in table_columns:
        try:
            rows = conn.execute(
                "SELECT migration_version,migration_digest,resulting_schema_fingerprint "
                "FROM schema_migrations "
                "ORDER BY migration_version DESC LIMIT 2"
            ).fetchall()
        except sqlite3.Error:
            marker_state = "invalid_marker"
        else:
            if rows:
                marker_state = "marked"
                migration_version = int(rows[0][0])
                migration_digest = str(rows[0][1])
                marker_resulting_fingerprint = str(rows[0][2])
            else:
                marker_state = "empty_marker"
    pragmas = {
        "user_version": int(conn.execute("PRAGMA user_version").fetchone()[0]),
        "application_id": int(conn.execute("PRAGMA application_id").fetchone()[0]),
        "schema_version": int(conn.execute("PRAGMA schema_version").fetchone()[0]),
        "page_size": int(conn.execute("PRAGMA page_size").fetchone()[0]),
        "journal_mode": str(conn.execute("PRAGMA journal_mode").fetchone()[0]),
        "foreign_keys": int(conn.execute("PRAGMA foreign_keys").fetchone()[0]),
    }
    return SchemaSnapshot(
        schema_fingerprint=_digest({"objects": objects}),
        object_names=tuple(name for _kind, name, _table, _sql in objects),
        object_counts=object_counts,
        object_sql=objects,
        table_columns=table_columns,
        row_counts=row_counts,
        id_set_digests=id_set_digests,
        critical_field_digests=critical_field_digests,
        pragmas=pragmas,
        marker_state=marker_state,
        migration_version=migration_version,
        migration_digest=migration_digest,
        marker_resulting_fingerprint=marker_resulting_fingerprint,
    )


def inspect_schema(db_path: str | os.PathLike[str]) -> SchemaSnapshot:
    """Open any source DB immutable/read-only and return a sanitized snapshot."""
    try:
        target = Path(db_path).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise MigrationRehearsalError("schema_source_unavailable") from exc
    if not target.is_file():
        raise MigrationRehearsalError("schema_source_not_file")
    try:
        suffix = "?mode=ro&immutable=1" if target == PRODUCTION_DATABASE else "?mode=ro"
        conn = sqlite3.connect(target.as_uri() + suffix, uri=True)
        conn.execute("PRAGMA query_only=ON")
        return inspect_schema_connection(conn)
    except sqlite3.Error as exc:
        raise MigrationRehearsalError("schema_inspection_failed") from exc
    finally:
        if "conn" in locals():
            conn.close()


def _table_ddl() -> tuple[str, ...]:
    return (
        """
        CREATE TABLE schema_migrations(
            migration_id TEXT PRIMARY KEY,
            migration_version INTEGER NOT NULL UNIQUE,
            migration_name TEXT NOT NULL,
            migration_digest TEXT NOT NULL,
            applied_at TEXT NOT NULL,
            application_version TEXT NOT NULL,
            previous_schema_fingerprint TEXT NOT NULL,
            resulting_schema_fingerprint TEXT NOT NULL,
            rehearsal_id TEXT NOT NULL,
            migration_schema_version TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE yunos_schema_meta(
            meta_key TEXT PRIMARY KEY,
            meta_value TEXT NOT NULL,
            value_digest TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            meta_schema_version TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE ingestion_source_manifests(
            manifest_id TEXT PRIMARY KEY,
            source_type TEXT NOT NULL,
            source_version TEXT NOT NULL,
            adapter_name TEXT NOT NULL,
            adapter_version TEXT NOT NULL,
            archive_sha256 TEXT NOT NULL,
            archive_size INTEGER NOT NULL CHECK(archive_size >= 0),
            archive_kind TEXT NOT NULL,
            manifest_schema_version TEXT NOT NULL,
            validation_status TEXT NOT NULL,
            record_counts_json TEXT NOT NULL CHECK(json_valid(record_counts_json)),
            asset_counts_json TEXT NOT NULL CHECK(json_valid(asset_counts_json)),
            warnings_json TEXT NOT NULL CHECK(json_valid(warnings_json)),
            created_at TEXT NOT NULL,
            scanned_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE ingestion_batches(
            batch_id TEXT PRIMARY KEY,
            manifest_id TEXT NOT NULL,
            batch_mode TEXT NOT NULL,
            current_state TEXT NOT NULL,
            plan_digest TEXT NOT NULL,
            approval_digest TEXT,
            target_snapshot_fingerprint TEXT NOT NULL,
            asset_stage_fingerprint TEXT NOT NULL,
            recovery_status TEXT NOT NULL,
            warning_count INTEGER NOT NULL CHECK(warning_count >= 0),
            error_count INTEGER NOT NULL CHECK(error_count >= 0),
            conflict_count INTEGER NOT NULL CHECK(conflict_count >= 0),
            batch_schema_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(manifest_id) REFERENCES ingestion_source_manifests(manifest_id)
        )
        """,
        """
        CREATE TABLE ingestion_batch_events(
            batch_id TEXT NOT NULL,
            sequence INTEGER NOT NULL CHECK(sequence >= 1),
            event_id TEXT NOT NULL UNIQUE,
            event_type TEXT NOT NULL,
            previous_state TEXT NOT NULL,
            next_state TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            actor_kind TEXT NOT NULL,
            reason_code TEXT NOT NULL,
            payload_digest TEXT NOT NULL,
            previous_event_digest TEXT NOT NULL,
            event_digest TEXT NOT NULL,
            event_schema_version TEXT NOT NULL,
            PRIMARY KEY(batch_id,sequence),
            FOREIGN KEY(batch_id) REFERENCES ingestion_batches(batch_id)
        )
        """,
        """
        CREATE TABLE ingestion_source_bindings(
            source_type TEXT NOT NULL,
            source_record_kind TEXT NOT NULL,
            source_record_id TEXT NOT NULL,
            target_record_kind TEXT NOT NULL,
            target_record_id TEXT NOT NULL,
            manifest_id TEXT NOT NULL,
            batch_id TEXT NOT NULL,
            source_digest TEXT NOT NULL,
            binding_state TEXT NOT NULL,
            binding_schema_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(source_type,source_record_kind,source_record_id),
            FOREIGN KEY(manifest_id) REFERENCES ingestion_source_manifests(manifest_id),
            FOREIGN KEY(batch_id) REFERENCES ingestion_batches(batch_id)
        )
        """,
        """
        CREATE TABLE ingestion_asset_entities(
            entity_digest TEXT PRIMARY KEY,
            detected_mime_type TEXT NOT NULL,
            actual_size INTEGER NOT NULL CHECK(actual_size >= 0),
            entity_state TEXT NOT NULL,
            storage_key TEXT NOT NULL CHECK(
                storage_key <> '' AND
                substr(storage_key,1,1) NOT IN ('/','\\') AND
                instr(storage_key,':') = 0 AND
                instr(storage_key,'..') = 0
            ),
            verification_digest TEXT NOT NULL,
            first_batch_id TEXT NOT NULL,
            entity_schema_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(first_batch_id) REFERENCES ingestion_batches(batch_id)
        )
        """,
        """
        CREATE TABLE ingestion_attachment_bindings(
            logical_media_id TEXT PRIMARY KEY,
            source_attachment_id_digest TEXT NOT NULL,
            message_id TEXT NOT NULL,
            entity_digest TEXT NOT NULL,
            manifest_id TEXT NOT NULL,
            batch_id TEXT NOT NULL,
            occurrence_index INTEGER NOT NULL CHECK(occurrence_index >= 0),
            attachment_state TEXT NOT NULL,
            binding_schema_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(source_attachment_id_digest,message_id,occurrence_index),
            FOREIGN KEY(entity_digest) REFERENCES ingestion_asset_entities(entity_digest),
            FOREIGN KEY(manifest_id) REFERENCES ingestion_source_manifests(manifest_id),
            FOREIGN KEY(batch_id) REFERENCES ingestion_batches(batch_id)
        )
        """,
        """
        CREATE TABLE ingestion_conflicts(
            conflict_id TEXT PRIMARY KEY,
            batch_id TEXT NOT NULL,
            conflict_type TEXT NOT NULL,
            severity TEXT NOT NULL,
            evidence_digest TEXT NOT NULL,
            proposed_actions_json TEXT NOT NULL CHECK(json_valid(proposed_actions_json)),
            default_action TEXT NOT NULL,
            requires_approval INTEGER NOT NULL CHECK(requires_approval IN (0,1)),
            conflict_state TEXT NOT NULL,
            conflict_schema_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(batch_id) REFERENCES ingestion_batches(batch_id)
        )
        """,
        """
        CREATE TABLE ingestion_decisions(
            decision_id TEXT PRIMARY KEY,
            conflict_id TEXT NOT NULL UNIQUE,
            selected_action TEXT NOT NULL,
            allowed_operation_ids_json TEXT NOT NULL CHECK(json_valid(allowed_operation_ids_json)),
            rejected_operation_ids_json TEXT NOT NULL CHECK(json_valid(rejected_operation_ids_json)),
            evidence_digest TEXT NOT NULL,
            decision_reason_code TEXT NOT NULL,
            actor_kind TEXT NOT NULL,
            decision_schema_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(conflict_id) REFERENCES ingestion_conflicts(conflict_id)
        )
        """,
        """
        CREATE TABLE ingestion_approvals(
            approval_id TEXT PRIMARY KEY,
            batch_id TEXT NOT NULL,
            plan_digest TEXT NOT NULL,
            stage_plan_digest TEXT NOT NULL,
            target_snapshot_fingerprint TEXT NOT NULL,
            approval_digest TEXT NOT NULL UNIQUE,
            approval_scope TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            actor_kind TEXT NOT NULL,
            approval_schema_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(batch_id) REFERENCES ingestion_batches(batch_id)
        )
        """,
        """
        CREATE TABLE ingestion_executions(
            execution_id TEXT PRIMARY KEY,
            batch_id TEXT NOT NULL,
            approval_digest TEXT NOT NULL,
            execution_state TEXT NOT NULL,
            database_before_fingerprint TEXT NOT NULL,
            database_after_fingerprint TEXT,
            asset_before_fingerprint TEXT NOT NULL,
            asset_after_fingerprint TEXT,
            verification_digest TEXT,
            recovery_status TEXT NOT NULL,
            execution_schema_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(batch_id) REFERENCES ingestion_batches(batch_id)
        )
        """,
        """
        CREATE TABLE ingestion_execution_events(
            execution_id TEXT NOT NULL,
            sequence INTEGER NOT NULL CHECK(sequence >= 1),
            event_id TEXT NOT NULL UNIQUE,
            event_type TEXT NOT NULL,
            previous_state TEXT NOT NULL,
            next_state TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            actor_kind TEXT NOT NULL,
            reason_code TEXT NOT NULL,
            payload_digest TEXT NOT NULL,
            previous_event_digest TEXT NOT NULL,
            event_digest TEXT NOT NULL,
            event_schema_version TEXT NOT NULL,
            PRIMARY KEY(execution_id,sequence),
            FOREIGN KEY(execution_id) REFERENCES ingestion_executions(execution_id)
        )
        """,
        """
        CREATE TABLE ingestion_verifications(
            verification_id TEXT PRIMARY KEY,
            execution_id TEXT NOT NULL UNIQUE,
            passed INTEGER NOT NULL CHECK(passed IN (0,1)),
            actual_deltas_json TEXT NOT NULL CHECK(json_valid(actual_deltas_json)),
            expected_deltas_json TEXT NOT NULL CHECK(json_valid(expected_deltas_json)),
            invariant_results_json TEXT NOT NULL CHECK(json_valid(invariant_results_json)),
            database_after_fingerprint TEXT NOT NULL,
            asset_after_fingerprint TEXT NOT NULL,
            verification_digest TEXT NOT NULL UNIQUE,
            verification_schema_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(execution_id) REFERENCES ingestion_executions(execution_id)
        )
        """,
        """
        CREATE TABLE ingestion_leases(
            scope_type TEXT NOT NULL,
            scope_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            secret_digest TEXT NOT NULL,
            acquired_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            lease_state TEXT NOT NULL,
            lease_schema_version TEXT NOT NULL,
            PRIMARY KEY(scope_type,scope_id)
        )
        """,
    )


def _index_ddl() -> tuple[str, ...]:
    return (
        "CREATE INDEX ingestion_manifests_archive_idx ON ingestion_source_manifests(archive_sha256)",
        "CREATE INDEX ingestion_batches_manifest_idx ON ingestion_batches(manifest_id)",
        "CREATE INDEX ingestion_batches_state_idx ON ingestion_batches(current_state)",
        "CREATE INDEX ingestion_events_type_idx ON ingestion_batch_events(event_type)",
        "CREATE INDEX ingestion_bindings_target_idx ON ingestion_source_bindings(target_record_kind,target_record_id)",
        "CREATE INDEX ingestion_attachment_message_idx ON ingestion_attachment_bindings(message_id)",
        "CREATE INDEX ingestion_conflicts_batch_idx ON ingestion_conflicts(batch_id)",
        "CREATE INDEX ingestion_approvals_batch_idx ON ingestion_approvals(batch_id)",
        "CREATE INDEX ingestion_executions_batch_idx ON ingestion_executions(batch_id)",
        "CREATE INDEX ingestion_execution_events_type_idx ON ingestion_execution_events(event_type)",
        "CREATE INDEX ingestion_leases_expiry_idx ON ingestion_leases(expires_at)",
    )


def _trigger_ddl() -> tuple[str, ...]:
    append_only = (
        "schema_migrations",
        "ingestion_batch_events",
        "ingestion_execution_events",
        "ingestion_decisions",
        "ingestion_approvals",
        "ingestion_verifications",
    )
    statements: list[str] = []
    for table in append_only:
        statements.extend((
            f"""
            CREATE TRIGGER {table}_no_update
            BEFORE UPDATE ON {table}
            BEGIN SELECT RAISE(ABORT,'{table}_append_only'); END
            """,
            f"""
            CREATE TRIGGER {table}_no_delete
            BEFORE DELETE ON {table}
            BEGIN SELECT RAISE(ABORT,'{table}_append_only'); END
            """,
        ))
    return tuple(statements)


MIGRATION_TABLE_DDL = tuple(_normalize_sql(sql) for sql in _table_ddl())
MIGRATION_INDEX_DDL = tuple(_normalize_sql(sql) for sql in _index_ddl())
MIGRATION_TRIGGER_DDL = tuple(_normalize_sql(sql) for sql in _trigger_ddl())
MIGRATION_DDL = MIGRATION_TABLE_DDL + MIGRATION_INDEX_DDL + MIGRATION_TRIGGER_DDL
MIGRATION_DIGEST = _digest({
    "migration_id": MIGRATION_ID,
    "migration_version": MIGRATION_VERSION,
    "migration_name": MIGRATION_NAME,
    "statements": list(MIGRATION_DDL),
    "user_version": MIGRATION_VERSION,
})


def _recreate_schema(snapshot: SchemaSnapshot) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("PRAGMA foreign_keys=OFF")
    objects = snapshot.object_sql
    for kind in ("table", "view", "index", "trigger"):
        for object_kind, _name, _table, sql in objects:
            if object_kind == kind and sql:
                conn.execute(sql)
    return conn


def _expected_target_fingerprint(snapshot: SchemaSnapshot) -> str:
    try:
        conn = _recreate_schema(snapshot)
        for statement in MIGRATION_DDL:
            conn.execute(statement)
        return schema_fingerprint(conn)
    except sqlite3.Error as exc:
        raise MigrationRehearsalError("source_schema_not_recreatable") from exc
    finally:
        if "conn" in locals():
            conn.close()


def _validate_legacy_contract(snapshot: SchemaSnapshot) -> None:
    required = {
        "conversations": {"conversation_id", "title", "update_time", "source_platform"},
        "messages": {
            "message_id", "conversation_id", "ordinal", "role", "text",
            "metadata_json", "source_platform",
        },
    }
    for table, columns in required.items():
        if table not in snapshot.table_columns:
            raise MigrationRehearsalError("legacy_schema_required_table_missing")
        if not columns.issubset(snapshot.table_columns[table]):
            raise MigrationRehearsalError("legacy_schema_required_column_missing")


def build_migration_plan(snapshot: SchemaSnapshot) -> MigrationPlan:
    if not isinstance(snapshot, SchemaSnapshot):
        raise MigrationRehearsalError("invalid_schema_snapshot")
    _validate_legacy_contract(snapshot)
    if snapshot.marker_state == "marked":
        if (
            snapshot.migration_version == MIGRATION_VERSION
            and snapshot.migration_digest == MIGRATION_DIGEST
        ):
            if snapshot.marker_resulting_fingerprint != snapshot.schema_fingerprint:
                raise MigrationRehearsalError(
                    "migrated_schema_drift", recovery_required=True,
                )
            return MigrationPlan(
                migration_id=MIGRATION_ID,
                migration_version=MIGRATION_VERSION,
                migration_name=MIGRATION_NAME,
                migration_digest=MIGRATION_DIGEST,
                source_schema_fingerprint=snapshot.schema_fingerprint,
                expected_target_schema_fingerprint=snapshot.schema_fingerprint,
                statement_digests=tuple(_digest(sql) for sql in MIGRATION_DDL),
                table_count=len(MIGRATION_TABLE_DDL),
                index_count=len(MIGRATION_INDEX_DDL),
                trigger_count=len(MIGRATION_TRIGGER_DDL),
                destructive_statement_count=0,
                estimated_size_growth_bytes=0,
            )
        if snapshot.migration_version is not None and snapshot.migration_version > MIGRATION_VERSION:
            raise MigrationRehearsalError("higher_unknown_schema_version")
        if snapshot.migration_version == MIGRATION_VERSION:
            raise MigrationRehearsalError("migration_digest_mismatch")
        raise MigrationRehearsalError("unknown_migration_history")
    if snapshot.marker_state != "legacy":
        raise MigrationRehearsalError("migration_marker_incomplete", recovery_required=True)
    collision = set(REQUIRED_TABLES).intersection(snapshot.object_names)
    if collision:
        if (
            "yunos_schema_meta" in collision
            or snapshot.pragmas["user_version"] >= MIGRATION_VERSION
            or len(collision) > 1
        ):
            raise MigrationRehearsalError(
                "migration_marker_missing", recovery_required=True,
            )
        raise MigrationRehearsalError("unknown_same_name_schema_object")
    if snapshot.pragmas["user_version"] > MIGRATION_VERSION:
        raise MigrationRehearsalError("higher_unknown_schema_version")
    if snapshot.pragmas["user_version"] == MIGRATION_VERSION:
        raise MigrationRehearsalError(
            "migration_marker_missing", recovery_required=True,
        )
    return MigrationPlan(
        migration_id=MIGRATION_ID,
        migration_version=MIGRATION_VERSION,
        migration_name=MIGRATION_NAME,
        migration_digest=MIGRATION_DIGEST,
        source_schema_fingerprint=snapshot.schema_fingerprint,
        expected_target_schema_fingerprint=_expected_target_fingerprint(snapshot),
        statement_digests=tuple(_digest(sql) for sql in MIGRATION_DDL),
        table_count=len(MIGRATION_TABLE_DDL),
        index_count=len(MIGRATION_INDEX_DDL),
        trigger_count=len(MIGRATION_TRIGGER_DDL),
        destructive_statement_count=0,
        estimated_size_growth_bytes=512 * 1024,
    )


def validate_migration_plan(plan: MigrationPlan) -> None:
    if not isinstance(plan, MigrationPlan):
        raise MigrationRehearsalError("invalid_migration_plan")
    if (
        plan.migration_id != MIGRATION_ID
        or plan.migration_version != MIGRATION_VERSION
        or plan.migration_name != MIGRATION_NAME
        or plan.migration_digest != MIGRATION_DIGEST
        or plan.plan_schema_version != PLAN_SCHEMA_VERSION
    ):
        raise MigrationRehearsalError("migration_plan_digest_mismatch")
    if plan.statement_digests != tuple(_digest(sql) for sql in MIGRATION_DDL):
        raise MigrationRehearsalError("migration_plan_statement_mismatch")
    if plan.destructive_statement_count != 0:
        raise MigrationRehearsalError("destructive_migration_forbidden")
    for statement in MIGRATION_DDL:
        if re.search(r"^\s*(DROP|ALTER|DELETE|UPDATE)\b", statement, re.IGNORECASE):
            raise MigrationRehearsalError("destructive_migration_forbidden")


def _marker_consistent(snapshot: SchemaSnapshot) -> bool:
    required = set(REQUIRED_TABLES)
    return (
        snapshot.marker_state == "marked"
        and snapshot.migration_version == MIGRATION_VERSION
        and snapshot.migration_digest == MIGRATION_DIGEST
        and snapshot.marker_resulting_fingerprint == snapshot.schema_fingerprint
        and required.issubset(snapshot.object_names)
        and snapshot.pragmas["user_version"] == MIGRATION_VERSION
    )


def verify_migrated_schema(
    db_path: str | os.PathLike[str],
    *,
    expected_fingerprint: str | None = None,
) -> dict[str, Any]:
    target = validate_clone_database_path(db_path)
    with closing(sqlite3.connect(target)) as conn:
        conn.execute("PRAGMA query_only=ON")
        snapshot = inspect_schema_connection(conn)
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_key_violations = list(conn.execute("PRAGMA foreign_key_check"))
        new_counts = {
            table: int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in INGESTION_TABLES
        }
    if not _marker_consistent(snapshot):
        raise MigrationRehearsalError("migrated_schema_marker_mismatch", recovery_required=True)
    if expected_fingerprint and snapshot.schema_fingerprint != expected_fingerprint:
        raise MigrationRehearsalError("migrated_schema_drift", recovery_required=True)
    if integrity != "ok":
        raise MigrationRehearsalError("integrity_check_failed", recovery_required=True)
    if foreign_key_violations:
        raise MigrationRehearsalError("foreign_key_check_failed", recovery_required=True)
    return {
        "schema_fingerprint": snapshot.schema_fingerprint,
        "integrity_check": integrity,
        "foreign_key_violation_count": len(foreign_key_violations),
        "required_table_count": len(REQUIRED_TABLES),
        "new_table_row_counts": new_counts,
    }


def _lock_path(target: Path) -> Path:
    return target.with_name("." + hashlib.sha256(str(target).encode("utf-8")).hexdigest() + ".v83f.lock")


def _acquire_lock(target: Path) -> tuple[int, Path]:
    lock = _lock_path(target)
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise MigrationRehearsalError("concurrent_migrator_blocked") from exc
    os.write(descriptor, b"v8.3-f")
    return descriptor, lock


def _release_lock(descriptor: int, lock: Path) -> None:
    os.close(descriptor)
    try:
        lock.unlink()
    except FileNotFoundError:
        pass


FaultInjector = Callable[[str, sqlite3.Connection], None]
MigrationEventHook = Callable[[str], None]


def _checkpoint(
    name: str,
    conn: sqlite3.Connection,
    fault_injector: FaultInjector | None,
) -> None:
    if fault_injector is not None:
        fault_injector(name, conn)


def _emit_migration_event(
    name: str,
    event_hook: MigrationEventHook | None,
) -> None:
    if event_hook is not None:
        event_hook(name)


def _apply_v8301_migration_to_validated_target(
    plan: MigrationPlan,
    target: Path,
    *,
    execution_id: str,
    fault_injector: FaultInjector | None = None,
    event_hook: MigrationEventHook | None = None,
    minimum_free_bytes: int = 2 * 1024 * 1024,
) -> MigrationResult:
    """Private exact-v8301 executor shared by clone and authorized entrypoints.

    Callers must validate the target path before reaching this function.  The
    public clone entrypoint below remains clone-only; the production caller is
    the fail-closed authorized protocol in production_migration_entry.py.
    """
    validate_migration_plan(plan)
    _identity(execution_id, "invalid_execution_id")
    if not isinstance(target, Path) or not target.is_absolute() or not target.is_file():
        raise MigrationRehearsalError("migration_target_not_validated")
    free_bytes = shutil.disk_usage(target.parent).free
    if free_bytes < minimum_free_bytes:
        raise MigrationRehearsalError("insufficient_disk_space")
    descriptor, lock = _acquire_lock(target)
    committed = False
    before: SchemaSnapshot | None = None
    try:
        _emit_migration_event("lease_acquired", event_hook)
        conn = sqlite3.connect(target, timeout=5.0, isolation_level=None)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        before = inspect_schema_connection(conn)
        if _marker_consistent(before):
            if before.schema_fingerprint != plan.expected_target_schema_fingerprint:
                raise MigrationRehearsalError("migrated_schema_drift", recovery_required=True)
            return MigrationResult(
                status="already_applied",
                migration_id=MIGRATION_ID,
                migration_version=MIGRATION_VERSION,
                migration_digest=MIGRATION_DIGEST,
                before_schema_fingerprint=before.schema_fingerprint,
                after_schema_fingerprint=before.schema_fingerprint,
                integrity_check="ok",
                foreign_key_violation_count=0,
                recovery_required=False,
            )
        if before.marker_state == "marked":
            if before.migration_version is not None and before.migration_version > MIGRATION_VERSION:
                raise MigrationRehearsalError("higher_unknown_schema_version")
            if before.migration_version == MIGRATION_VERSION:
                raise MigrationRehearsalError("migration_digest_mismatch")
            raise MigrationRehearsalError("unknown_migration_history")
        if before.marker_state != "legacy":
            raise MigrationRehearsalError("migration_marker_incomplete", recovery_required=True)
        if before.schema_fingerprint != plan.source_schema_fingerprint:
            raise MigrationRehearsalError("migration_source_schema_drift")
        if set(REQUIRED_TABLES).intersection(before.object_names):
            raise MigrationRehearsalError("unknown_same_name_schema_object")

        conn.execute("BEGIN IMMEDIATE")
        _emit_migration_event("transaction_started", event_hook)
        try:
            for index, statement in enumerate(MIGRATION_TABLE_DDL):
                conn.execute(statement)
                if index == 1:
                    _checkpoint("marker_created", conn, fault_injector)
                if index == 5:
                    _checkpoint("partial_tables_created", conn, fault_injector)
            _checkpoint("tables_created", conn, fault_injector)
            for statement in MIGRATION_INDEX_DDL:
                conn.execute(statement)
            _checkpoint("indexes_created", conn, fault_injector)
            for statement in MIGRATION_TRIGGER_DDL:
                conn.execute(statement)
            _checkpoint("triggers_created", conn, fault_injector)
            _emit_migration_event("migration_applied", event_hook)
            conn.execute(f"PRAGMA user_version={MIGRATION_VERSION}")
            target_fingerprint = schema_fingerprint(conn)
            if target_fingerprint != plan.expected_target_schema_fingerprint:
                raise MigrationRehearsalError("target_schema_fingerprint_mismatch")
            applied_at = utc_now()
            conn.execute(
                "INSERT INTO schema_migrations VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    MIGRATION_ID,
                    MIGRATION_VERSION,
                    MIGRATION_NAME,
                    MIGRATION_DIGEST,
                    applied_at,
                    APPLICATION_VERSION,
                    before.schema_fingerprint,
                    target_fingerprint,
                    execution_id,
                    MIGRATION_SCHEMA_VERSION,
                ),
            )
            meta_value = _canonical_text({
                "application_version": APPLICATION_VERSION,
                "migration_digest": MIGRATION_DIGEST,
                "migration_version": MIGRATION_VERSION,
                "schema_fingerprint": target_fingerprint,
            })
            conn.execute(
                "INSERT INTO yunos_schema_meta VALUES(?,?,?,?,?)",
                (
                    "ingestion_schema",
                    meta_value,
                    hashlib.sha256(meta_value.encode("utf-8")).hexdigest(),
                    applied_at,
                    MIGRATION_SCHEMA_VERSION,
                ),
            )
            _emit_migration_event("marker_written", event_hook)
            _checkpoint("before_commit", conn, fault_injector)
            conn.execute("COMMIT")
            committed = True
            _emit_migration_event("transaction_committed", event_hook)
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

        _emit_migration_event("verification_started", event_hook)
        _checkpoint("after_commit_report", conn, fault_injector)
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        _checkpoint("integrity_checked", conn, fault_injector)
        foreign_key_violations = list(conn.execute("PRAGMA foreign_key_check"))
        _checkpoint("foreign_keys_checked", conn, fault_injector)
        after = inspect_schema_connection(conn)
        if integrity != "ok":
            raise MigrationRehearsalError("integrity_check_failed", recovery_required=True)
        if foreign_key_violations:
            raise MigrationRehearsalError("foreign_key_check_failed", recovery_required=True)
        if not _marker_consistent(after) or after.schema_fingerprint != target_fingerprint:
            raise MigrationRehearsalError("post_commit_schema_verification_failed", recovery_required=True)
        _emit_migration_event("verification_passed", event_hook)
        return MigrationResult(
            status="completed",
            migration_id=MIGRATION_ID,
            migration_version=MIGRATION_VERSION,
            migration_digest=MIGRATION_DIGEST,
            before_schema_fingerprint=before.schema_fingerprint,
            after_schema_fingerprint=after.schema_fingerprint,
            integrity_check=integrity,
            foreign_key_violation_count=0,
            recovery_required=False,
        )
    except MigrationRehearsalError:
        raise
    except MigrationInterrupted:
        raise
    except sqlite3.Error as exc:
        raise MigrationRehearsalError(
            "migration_sql_failed",
            recovery_required=committed,
        ) from exc
    except OSError as exc:
        raise MigrationRehearsalError(
            "migration_io_failed",
            recovery_required=committed,
        ) from exc
    finally:
        if "conn" in locals():
            conn.close()
        _release_lock(descriptor, lock)


def apply_migration_to_clone(
    plan: MigrationPlan,
    clone_path: str | os.PathLike[str],
    *,
    rehearsal_id: str,
    fault_injector: FaultInjector | None = None,
    event_hook: MigrationEventHook | None = None,
    minimum_free_bytes: int = 2 * 1024 * 1024,
) -> MigrationResult:
    """Apply the fixed additive migration to one disposable clone."""
    validate_migration_plan(plan)
    _identity(rehearsal_id, "invalid_rehearsal_id")
    target = validate_clone_database_path(clone_path)
    return _apply_v8301_migration_to_validated_target(
        plan,
        target,
        execution_id=rehearsal_id,
        fault_injector=fault_injector,
        event_hook=event_hook,
        minimum_free_bytes=minimum_free_bytes,
    )


def inject_migration_failure(checkpoint: str, *, crash: bool = False) -> FaultInjector:
    allowed = {
        "marker_created",
        "partial_tables_created",
        "tables_created",
        "indexes_created",
        "triggers_created",
        "before_commit",
        "after_commit_report",
        "integrity_checked",
        "foreign_keys_checked",
    }
    if checkpoint not in allowed:
        raise MigrationRehearsalError("unknown_failure_checkpoint")

    def injector(name: str, _conn: sqlite3.Connection) -> None:
        if name == checkpoint:
            if crash:
                raise MigrationInterrupted(name)
            raise MigrationRehearsalError(
                f"injected_{name}_failure",
                recovery_required=name in {
                    "after_commit_report", "integrity_checked", "foreign_keys_checked",
                },
            )

    return injector


def build_production_migration_approval_package(
    *,
    plan: MigrationPlan,
    current_production_fingerprint: str,
    compatibility_summary: Mapping[str, Any],
    failure_injection_summary: Mapping[str, Any],
    rollback_summary: Mapping[str, Any],
    rehearsal_run_count: int,
    expires_at: str,
) -> dict[str, Any]:
    """Build a path-free package.  This does not approve or execute migration."""
    validate_migration_plan(plan)
    _sha(current_production_fingerprint, "invalid_production_fingerprint")
    if rehearsal_run_count < 1:
        raise MigrationRehearsalError("rehearsal_run_required")
    package = {
        "package_schema_version": APPROVAL_PACKAGE_SCHEMA_VERSION,
        "status": "awaiting_explicit_human_approval",
        "production_execution_authorized": False,
        "migration_id": plan.migration_id,
        "migration_version": plan.migration_version,
        "migration_digest": plan.migration_digest,
        "source_schema_fingerprint": plan.source_schema_fingerprint,
        "expected_target_schema_fingerprint": plan.expected_target_schema_fingerprint,
        "current_production_schema_fingerprint": current_production_fingerprint,
        "backup_plan": {
            "method": "sqlite_online_backup",
            "verify_before_use": True,
            "restore_requires_service_stop": True,
        },
        "estimated_objects_added": {
            "tables": plan.table_count,
            "indexes": plan.index_count,
            "triggers": plan.trigger_count,
        },
        "destructive_changes": {
            "tables_deleted": 0,
            "legacy_columns_changed": 0,
            "legacy_rows_rewritten": 0,
        },
        "estimated_size_growth_bytes": plan.estimated_size_growth_bytes,
        "compatibility_summary_digest": _digest(compatibility_summary),
        "rehearsal_run_count": rehearsal_run_count,
        "failure_injection_summary_digest": _digest(failure_injection_summary),
        "rollback_summary_digest": _digest(rollback_summary),
        "known_risks": [
            "exclusive_writer_window_required",
            "operator_must_reconfirm_live_fingerprints",
            "post_migration_human_acceptance_required",
        ],
        "human_stop_points": [
            "before_backup",
            "after_backup_verification",
            "before_begin_immediate",
            "after_schema_verification",
            "before_service_resume",
        ],
        "execution_command_summary": "disabled_gate_only_no_shortcut_command",
        "rollback_command_summary": "verified_backup_restore_with_manual_stop",
        "approval_expires_at": expires_at,
        "operator_checklist": [
            "formal_v8_3_f_commit_exists",
            "migration_digest_matches",
            "all_rehearsals_pass",
            "live_db_wal_shm_reconfirmed",
            "imports_tree_reconfirmed",
            "mobile_and_remote_access_services_controlled",
            "explicit_user_approval_recorded",
        ],
    }
    package["package_digest"] = _digest(package)
    return package


def apply_production_migration(*_args: Any, **_kwargs: Any) -> None:
    """Permanent disabled gate; production approval is outside v8.3-F."""
    raise MigrationRehearsalError("production_migration_not_authorized")


def downgrade_schema(*_args: Any, **_kwargs: Any) -> None:
    raise MigrationRehearsalError("automatic_downgrade_not_supported")


# ---------------------------------------------------------------------------
# YunOS v8.3-G0 / migration 8302
#
# v8301 above is intentionally frozen.  v8302 is an additive successor that
# creates an independent historical-reconstruction chain; it never weakens the
# real-archive SourceManifest or reuses its batch root.
# ---------------------------------------------------------------------------

HISTORICAL_MIGRATION_ID = "migration_yunos_v8_3_historical_reconstruction_002"
HISTORICAL_MIGRATION_VERSION = 8302
HISTORICAL_MIGRATION_NAME = "yunos_v8_3_historical_reconstruction"
HISTORICAL_MIGRATION_SCHEMA_VERSION = "v8.3-g0-migration-marker-1"
HISTORICAL_APPLICATION_VERSION = "YunOS-v8.3-G0"
HISTORICAL_PLAN_SCHEMA_VERSION = "v8.3-g0-migration-plan-1"
HISTORICAL_RESULT_SCHEMA_VERSION = "v8.3-g0-migration-result-1"

HISTORICAL_INGESTION_TABLES = (
    "ingestion_historical_manifests",
    "ingestion_historical_batches",
    "ingestion_historical_batch_events",
    "ingestion_historical_source_bindings",
    "ingestion_historical_attachment_bindings",
    "ingestion_historical_conflicts",
    "ingestion_historical_decisions",
    "ingestion_historical_approvals",
    "ingestion_historical_executions",
    "ingestion_historical_execution_events",
    "ingestion_historical_verifications",
)


def _historical_table_ddl() -> tuple[str, ...]:
    return (
        """
        CREATE TABLE ingestion_historical_manifests(
            historical_manifest_id TEXT PRIMARY KEY,
            source_type TEXT NOT NULL CHECK(
                source_type <> '' AND
                instr(source_type,'/') = 0 AND
                instr(source_type,'\\') = 0
            ),
            provenance_kind TEXT NOT NULL CHECK(provenance_kind IN (
                'deterministic_legacy','probable_legacy','legacy_unknown',
                'conflicting_evidence'
            )),
            archive_kind TEXT NOT NULL CHECK(archive_kind IN (
                'unavailable','verified_archive'
            )),
            archive_sha256 TEXT,
            archive_size INTEGER CHECK(archive_size IS NULL OR archive_size >= 0),
            adapter_name TEXT,
            adapter_version TEXT,
            evidence_policy_version TEXT NOT NULL,
            evidence_digest TEXT NOT NULL CHECK(
                length(evidence_digest)=64 AND
                evidence_digest NOT GLOB '*[^0-9a-f]*' AND
                evidence_digest <> '0000000000000000000000000000000000000000000000000000000000000000'
            ),
            reconstruction_digest TEXT NOT NULL CHECK(
                length(reconstruction_digest)=64 AND
                reconstruction_digest NOT GLOB '*[^0-9a-f]*' AND
                reconstruction_digest <> '0000000000000000000000000000000000000000000000000000000000000000'
            ),
            validation_status TEXT NOT NULL,
            record_counts_json TEXT NOT NULL CHECK(
                json_valid(record_counts_json) AND
                json(record_counts_json)=record_counts_json
            ),
            asset_counts_json TEXT NOT NULL CHECK(
                json_valid(asset_counts_json) AND
                json(asset_counts_json)=asset_counts_json
            ),
            warnings_json TEXT NOT NULL CHECK(
                json_valid(warnings_json) AND
                json(warnings_json)=warnings_json
            ),
            created_at TEXT NOT NULL,
            manifest_schema_version TEXT NOT NULL,
            CHECK(
                (
                    archive_kind='unavailable' AND
                    archive_sha256 IS NULL AND
                    archive_size IS NULL
                ) OR (
                    archive_kind='verified_archive' AND
                    archive_sha256 IS NOT NULL AND
                    length(archive_sha256)=64 AND
                    archive_sha256 NOT GLOB '*[^0-9a-f]*' AND
                    archive_sha256 <>
                    '0000000000000000000000000000000000000000000000000000000000000000'
                    AND archive_size IS NOT NULL
                )
            ),
            CHECK(
                (adapter_name IS NULL AND adapter_version IS NULL) OR
                (
                    adapter_name IS NOT NULL AND adapter_name <> '' AND
                    adapter_version IS NOT NULL AND adapter_version <> ''
                )
            ),
            CHECK(archive_sha256 IS NULL OR archive_sha256 <> '')
        )
        """,
        """
        CREATE TABLE ingestion_historical_batches(
            historical_batch_id TEXT PRIMARY KEY,
            historical_manifest_id TEXT NOT NULL,
            batch_mode TEXT NOT NULL CHECK(batch_mode='historical_backfill'),
            current_state TEXT NOT NULL,
            plan_digest TEXT NOT NULL CHECK(
                length(plan_digest)=64 AND
                plan_digest NOT GLOB '*[^0-9a-f]*'
            ),
            approval_digest TEXT CHECK(
                approval_digest IS NULL OR (
                    length(approval_digest)=64 AND
                    approval_digest NOT GLOB '*[^0-9a-f]*'
                )
            ),
            target_snapshot_fingerprint TEXT NOT NULL CHECK(
                length(target_snapshot_fingerprint)=64 AND
                target_snapshot_fingerprint NOT GLOB '*[^0-9a-f]*'
            ),
            recovery_status TEXT NOT NULL,
            warning_count INTEGER NOT NULL CHECK(warning_count >= 0),
            error_count INTEGER NOT NULL CHECK(error_count >= 0),
            conflict_count INTEGER NOT NULL CHECK(conflict_count >= 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            batch_schema_version TEXT NOT NULL,
            FOREIGN KEY(historical_manifest_id)
                REFERENCES ingestion_historical_manifests(historical_manifest_id)
        )
        """,
        """
        CREATE TABLE ingestion_historical_batch_events(
            historical_batch_id TEXT NOT NULL,
            sequence INTEGER NOT NULL CHECK(sequence >= 1),
            event_id TEXT NOT NULL UNIQUE,
            event_type TEXT NOT NULL,
            previous_state TEXT NOT NULL,
            next_state TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            actor_kind TEXT NOT NULL,
            reason_code TEXT NOT NULL,
            payload_digest TEXT NOT NULL CHECK(
                length(payload_digest)=64 AND
                payload_digest NOT GLOB '*[^0-9a-f]*'
            ),
            previous_event_digest TEXT NOT NULL CHECK(
                length(previous_event_digest)=64 AND
                previous_event_digest NOT GLOB '*[^0-9a-f]*'
            ),
            event_digest TEXT NOT NULL UNIQUE CHECK(
                length(event_digest)=64 AND
                event_digest NOT GLOB '*[^0-9a-f]*'
            ),
            event_schema_version TEXT NOT NULL,
            PRIMARY KEY(historical_batch_id,sequence),
            FOREIGN KEY(historical_batch_id)
                REFERENCES ingestion_historical_batches(historical_batch_id)
        )
        """,
        """
        CREATE TABLE ingestion_historical_source_bindings(
            binding_id TEXT PRIMARY KEY,
            historical_batch_id TEXT NOT NULL,
            target_record_kind TEXT NOT NULL CHECK(
                target_record_kind IN ('conversation','message')
            ),
            target_record_id TEXT NOT NULL CHECK(
                target_record_id <> '' AND
                substr(target_record_id,1,1) NOT IN ('/','\\') AND
                NOT (
                    length(target_record_id) >= 3 AND
                    substr(target_record_id,2,1)=':' AND
                    substr(target_record_id,3,1) IN ('/','\\')
                ) AND
                instr(target_record_id,char(0))=0
            ),
            proposed_source_type TEXT NOT NULL,
            source_record_id_digest TEXT CHECK(
                source_record_id_digest IS NULL OR
                (
                    length(source_record_id_digest)=64 AND
                    source_record_id_digest NOT GLOB '*[^0-9a-f]*' AND
                    source_record_id_digest <>
                    '0000000000000000000000000000000000000000000000000000000000000000'
                )
            ),
            provenance_kind TEXT NOT NULL CHECK(provenance_kind IN (
                'deterministic_legacy','probable_legacy','legacy_unknown',
                'conflicting_evidence'
            )),
            evidence_digest TEXT NOT NULL CHECK(
                length(evidence_digest)=64 AND
                evidence_digest NOT GLOB '*[^0-9a-f]*'
            ),
            binding_state TEXT NOT NULL CHECK(binding_state IN (
                'approved','applied','verified','deferred','rejected',
                'recorded_unknown','blocked'
            )),
            created_at TEXT NOT NULL,
            binding_schema_version TEXT NOT NULL,
            FOREIGN KEY(historical_batch_id)
                REFERENCES ingestion_historical_batches(historical_batch_id),
            CHECK(
                provenance_kind <> 'legacy_unknown' OR
                (
                    source_record_id_digest IS NULL AND
                    binding_state='recorded_unknown'
                )
            ),
            CHECK(
                provenance_kind <> 'probable_legacy' OR
                binding_state IN ('deferred','rejected')
            ),
            CHECK(
                provenance_kind <> 'conflicting_evidence' OR
                binding_state='blocked'
            ),
            CHECK(
                provenance_kind <> 'deterministic_legacy' OR
                source_record_id_digest IS NOT NULL
            )
        )
        """,
        """
        CREATE TABLE ingestion_historical_attachment_bindings(
            binding_id TEXT PRIMARY KEY,
            historical_batch_id TEXT NOT NULL,
            logical_media_id TEXT NOT NULL CHECK(
                logical_media_id GLOB 'ym1_[a-z2-7]*' AND
                logical_media_id NOT GLOB '*[^a-z0-9_]*'
            ),
            source_attachment_id_digest TEXT CHECK(
                source_attachment_id_digest IS NULL OR
                (
                    length(source_attachment_id_digest)=64 AND
                    source_attachment_id_digest NOT GLOB '*[^0-9a-f]*' AND
                    source_attachment_id_digest <>
                    '0000000000000000000000000000000000000000000000000000000000000000'
                )
            ),
            message_id TEXT NOT NULL CHECK(
                message_id <> '' AND
                substr(message_id,1,1) NOT IN ('/','\\') AND
                NOT (
                    length(message_id) >= 3 AND
                    substr(message_id,2,1)=':' AND
                    substr(message_id,3,1) IN ('/','\\')
                ) AND
                instr(message_id,char(0))=0
            ),
            entity_digest TEXT CHECK(
                entity_digest IS NULL OR
                (
                    length(entity_digest)=64 AND
                    entity_digest NOT GLOB '*[^0-9a-f]*' AND
                    entity_digest <>
                    '0000000000000000000000000000000000000000000000000000000000000000'
                )
            ),
            provenance_kind TEXT NOT NULL CHECK(provenance_kind IN (
                'deterministic_legacy','probable_legacy','legacy_unknown',
                'conflicting_evidence'
            )),
            attachment_state TEXT NOT NULL CHECK(attachment_state IN (
                'asset_record_missing','entity_missing','metadata_only',
                'size_conflict_unresolved','entity_available'
            )),
            size_conflict_state TEXT NOT NULL CHECK(size_conflict_state IN (
                'clear','size_conflict_unresolved'
            )),
            evidence_digest TEXT NOT NULL CHECK(
                length(evidence_digest)=64 AND
                evidence_digest NOT GLOB '*[^0-9a-f]*'
            ),
            occurrence_index INTEGER NOT NULL CHECK(occurrence_index >= 0),
            created_at TEXT NOT NULL,
            binding_schema_version TEXT NOT NULL,
            UNIQUE(
                historical_batch_id,logical_media_id,message_id,occurrence_index
            ),
            FOREIGN KEY(historical_batch_id)
                REFERENCES ingestion_historical_batches(historical_batch_id),
            CHECK(
                attachment_state NOT IN (
                    'asset_record_missing','entity_missing','metadata_only',
                    'size_conflict_unresolved'
                ) OR entity_digest IS NULL
            ),
            CHECK(
                attachment_state <> 'entity_available' OR
                entity_digest IS NOT NULL
            ),
            CHECK(
                size_conflict_state <> 'size_conflict_unresolved' OR
                entity_digest IS NULL
            )
        )
        """,
        """
        CREATE TABLE ingestion_historical_conflicts(
            conflict_id TEXT PRIMARY KEY,
            historical_batch_id TEXT NOT NULL,
            conflict_type TEXT NOT NULL,
            severity TEXT NOT NULL,
            evidence_digest TEXT NOT NULL CHECK(
                length(evidence_digest)=64 AND
                evidence_digest NOT GLOB '*[^0-9a-f]*'
            ),
            proposed_actions_json TEXT NOT NULL CHECK(
                json_valid(proposed_actions_json) AND
                json(proposed_actions_json)=proposed_actions_json
            ),
            default_action TEXT NOT NULL,
            requires_approval INTEGER NOT NULL CHECK(requires_approval IN (0,1)),
            conflict_state TEXT NOT NULL,
            conflict_schema_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(historical_batch_id)
                REFERENCES ingestion_historical_batches(historical_batch_id)
        )
        """,
        """
        CREATE TABLE ingestion_historical_decisions(
            decision_id TEXT PRIMARY KEY,
            conflict_id TEXT NOT NULL UNIQUE,
            selected_action TEXT NOT NULL,
            allowed_binding_ids_json TEXT NOT NULL CHECK(
                json_valid(allowed_binding_ids_json) AND
                json(allowed_binding_ids_json)=allowed_binding_ids_json
            ),
            rejected_binding_ids_json TEXT NOT NULL CHECK(
                json_valid(rejected_binding_ids_json) AND
                json(rejected_binding_ids_json)=rejected_binding_ids_json
            ),
            evidence_digest TEXT NOT NULL CHECK(
                length(evidence_digest)=64 AND
                evidence_digest NOT GLOB '*[^0-9a-f]*'
            ),
            decision_reason_code TEXT NOT NULL,
            actor_kind TEXT NOT NULL,
            decision_schema_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(conflict_id)
                REFERENCES ingestion_historical_conflicts(conflict_id)
        )
        """,
        """
        CREATE TABLE ingestion_historical_approvals(
            approval_id TEXT PRIMARY KEY,
            historical_batch_id TEXT NOT NULL,
            historical_manifest_id TEXT NOT NULL,
            plan_digest TEXT NOT NULL CHECK(
                length(plan_digest)=64 AND
                plan_digest NOT GLOB '*[^0-9a-f]*'
            ),
            target_snapshot_fingerprint TEXT NOT NULL CHECK(
                length(target_snapshot_fingerprint)=64 AND
                target_snapshot_fingerprint NOT GLOB '*[^0-9a-f]*'
            ),
            evidence_policy_version TEXT NOT NULL,
            evidence_digest TEXT NOT NULL CHECK(
                length(evidence_digest)=64 AND
                evidence_digest NOT GLOB '*[^0-9a-f]*'
            ),
            approval_digest TEXT NOT NULL UNIQUE CHECK(
                length(approval_digest)=64 AND
                approval_digest NOT GLOB '*[^0-9a-f]*'
            ),
            approval_scope TEXT NOT NULL CHECK(approval_scope='exact_plan'),
            expires_at TEXT NOT NULL,
            actor_kind TEXT NOT NULL,
            approval_schema_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(historical_batch_id)
                REFERENCES ingestion_historical_batches(historical_batch_id),
            FOREIGN KEY(historical_manifest_id)
                REFERENCES ingestion_historical_manifests(historical_manifest_id)
        )
        """,
        """
        CREATE TABLE ingestion_historical_executions(
            execution_id TEXT PRIMARY KEY,
            historical_batch_id TEXT NOT NULL,
            approval_digest TEXT NOT NULL,
            execution_state TEXT NOT NULL,
            database_before_fingerprint TEXT NOT NULL CHECK(
                length(database_before_fingerprint)=64 AND
                database_before_fingerprint NOT GLOB '*[^0-9a-f]*'
            ),
            database_after_fingerprint TEXT CHECK(
                database_after_fingerprint IS NULL OR
                (
                    length(database_after_fingerprint)=64 AND
                    database_after_fingerprint NOT GLOB '*[^0-9a-f]*'
                )
            ),
            verification_digest TEXT CHECK(
                verification_digest IS NULL OR (
                    length(verification_digest)=64 AND
                    verification_digest NOT GLOB '*[^0-9a-f]*'
                )
            ),
            recovery_status TEXT NOT NULL,
            execution_schema_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(historical_batch_id)
                REFERENCES ingestion_historical_batches(historical_batch_id),
            FOREIGN KEY(approval_digest)
                REFERENCES ingestion_historical_approvals(approval_digest)
        )
        """,
        """
        CREATE TABLE ingestion_historical_execution_events(
            execution_id TEXT NOT NULL,
            sequence INTEGER NOT NULL CHECK(sequence >= 1),
            event_id TEXT NOT NULL UNIQUE,
            event_type TEXT NOT NULL,
            previous_state TEXT NOT NULL,
            next_state TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            actor_kind TEXT NOT NULL,
            reason_code TEXT NOT NULL,
            payload_digest TEXT NOT NULL CHECK(
                length(payload_digest)=64 AND
                payload_digest NOT GLOB '*[^0-9a-f]*'
            ),
            previous_event_digest TEXT NOT NULL CHECK(
                length(previous_event_digest)=64 AND
                previous_event_digest NOT GLOB '*[^0-9a-f]*'
            ),
            event_digest TEXT NOT NULL UNIQUE CHECK(
                length(event_digest)=64 AND
                event_digest NOT GLOB '*[^0-9a-f]*'
            ),
            event_schema_version TEXT NOT NULL,
            PRIMARY KEY(execution_id,sequence),
            FOREIGN KEY(execution_id)
                REFERENCES ingestion_historical_executions(execution_id)
        )
        """,
        """
        CREATE TABLE ingestion_historical_verifications(
            verification_id TEXT PRIMARY KEY,
            execution_id TEXT NOT NULL UNIQUE,
            historical_batch_id TEXT NOT NULL,
            plan_digest TEXT NOT NULL CHECK(
                length(plan_digest)=64 AND
                plan_digest NOT GLOB '*[^0-9a-f]*'
            ),
            target_snapshot_fingerprint TEXT NOT NULL CHECK(
                length(target_snapshot_fingerprint)=64 AND
                target_snapshot_fingerprint NOT GLOB '*[^0-9a-f]*'
            ),
            evidence_policy_version TEXT NOT NULL,
            evidence_digest TEXT NOT NULL CHECK(
                length(evidence_digest)=64 AND
                evidence_digest NOT GLOB '*[^0-9a-f]*'
            ),
            passed INTEGER NOT NULL CHECK(passed IN (0,1)),
            invariant_results_json TEXT NOT NULL CHECK(
                json_valid(invariant_results_json) AND
                json(invariant_results_json)=invariant_results_json
            ),
            database_after_fingerprint TEXT NOT NULL CHECK(
                length(database_after_fingerprint)=64 AND
                database_after_fingerprint NOT GLOB '*[^0-9a-f]*'
            ),
            verification_digest TEXT NOT NULL UNIQUE CHECK(
                length(verification_digest)=64 AND
                verification_digest NOT GLOB '*[^0-9a-f]*'
            ),
            verification_schema_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(execution_id)
                REFERENCES ingestion_historical_executions(execution_id),
            FOREIGN KEY(historical_batch_id)
                REFERENCES ingestion_historical_batches(historical_batch_id)
        )
        """,
    )


def _historical_index_ddl() -> tuple[str, ...]:
    return (
        "CREATE INDEX idx_historical_manifests_evidence "
        "ON ingestion_historical_manifests(evidence_digest)",
        "CREATE INDEX idx_historical_batches_plan "
        "ON ingestion_historical_batches(plan_digest,target_snapshot_fingerprint)",
        "CREATE INDEX idx_historical_batch_events_event "
        "ON ingestion_historical_batch_events(event_digest)",
        "CREATE INDEX idx_historical_source_bindings_target "
        "ON ingestion_historical_source_bindings("
        "target_record_kind,target_record_id)",
        "CREATE INDEX idx_historical_source_bindings_source "
        "ON ingestion_historical_source_bindings("
        "proposed_source_type,source_record_id_digest)",
        "CREATE INDEX idx_historical_attachment_bindings_message "
        "ON ingestion_historical_attachment_bindings(message_id)",
        "CREATE INDEX idx_historical_attachment_bindings_media "
        "ON ingestion_historical_attachment_bindings(logical_media_id)",
        "CREATE INDEX idx_historical_conflicts_batch "
        "ON ingestion_historical_conflicts(historical_batch_id,conflict_state)",
        "CREATE INDEX idx_historical_executions_batch "
        "ON ingestion_historical_executions("
        "historical_batch_id,execution_state)",
    )


def _historical_trigger_ddl() -> tuple[str, ...]:
    append_only = (
        "ingestion_historical_manifests",
        "ingestion_historical_batch_events",
        "ingestion_historical_source_bindings",
        "ingestion_historical_attachment_bindings",
        "ingestion_historical_conflicts",
        "ingestion_historical_decisions",
        "ingestion_historical_approvals",
        "ingestion_historical_execution_events",
        "ingestion_historical_verifications",
    )
    statements: list[str] = []
    for table in append_only:
        statements.extend((
            f"""
            CREATE TRIGGER {table}_no_update
            BEFORE UPDATE ON {table}
            BEGIN SELECT RAISE(ABORT,'{table}_append_only'); END
            """,
            f"""
            CREATE TRIGGER {table}_no_delete
            BEFORE DELETE ON {table}
            BEGIN SELECT RAISE(ABORT,'{table}_append_only'); END
            """,
        ))
    statements.extend((
        """
        CREATE TRIGGER ingestion_historical_batches_no_delete
        BEFORE DELETE ON ingestion_historical_batches
        BEGIN
            SELECT RAISE(ABORT,'ingestion_historical_batches_append_only');
        END
        """,
        """
        CREATE TRIGGER ingestion_historical_batches_immutable_identity
        BEFORE UPDATE ON ingestion_historical_batches
        WHEN OLD.historical_batch_id<>NEW.historical_batch_id
          OR OLD.historical_manifest_id<>NEW.historical_manifest_id
          OR OLD.batch_mode<>NEW.batch_mode
          OR OLD.plan_digest<>NEW.plan_digest
          OR OLD.target_snapshot_fingerprint<>
             NEW.target_snapshot_fingerprint
          OR OLD.created_at<>NEW.created_at
          OR OLD.batch_schema_version<>NEW.batch_schema_version
        BEGIN
            SELECT RAISE(
                ABORT,'ingestion_historical_batches_immutable_identity'
            );
        END
        """,
        """
        CREATE TRIGGER ingestion_historical_executions_no_delete
        BEFORE DELETE ON ingestion_historical_executions
        BEGIN
            SELECT RAISE(ABORT,'ingestion_historical_executions_append_only');
        END
        """,
        """
        CREATE TRIGGER ingestion_historical_executions_immutable_identity
        BEFORE UPDATE ON ingestion_historical_executions
        WHEN OLD.execution_id<>NEW.execution_id
          OR OLD.historical_batch_id<>NEW.historical_batch_id
          OR OLD.approval_digest<>NEW.approval_digest
          OR OLD.database_before_fingerprint<>
             NEW.database_before_fingerprint
          OR OLD.created_at<>NEW.created_at
          OR OLD.execution_schema_version<>NEW.execution_schema_version
        BEGIN
            SELECT RAISE(
                ABORT,'ingestion_historical_executions_immutable_identity'
            );
        END
        """,
        """
        CREATE TRIGGER ingestion_historical_batch_events_chain
        BEFORE INSERT ON ingestion_historical_batch_events
        WHEN (
            NEW.sequence=1 AND
            NEW.previous_event_digest<>
            '0000000000000000000000000000000000000000000000000000000000000000'
        ) OR (
            NEW.sequence>1 AND NOT EXISTS(
                SELECT 1
                FROM ingestion_historical_batch_events AS previous
                WHERE previous.historical_batch_id=NEW.historical_batch_id
                  AND previous.sequence=NEW.sequence-1
                  AND previous.event_digest=NEW.previous_event_digest
            )
        )
        BEGIN
            SELECT RAISE(ABORT,'historical_batch_event_chain_mismatch');
        END
        """,
        """
        CREATE TRIGGER ingestion_historical_execution_events_chain
        BEFORE INSERT ON ingestion_historical_execution_events
        WHEN (
            NEW.sequence=1 AND
            NEW.previous_event_digest<>
            '0000000000000000000000000000000000000000000000000000000000000000'
        ) OR (
            NEW.sequence>1 AND NOT EXISTS(
                SELECT 1
                FROM ingestion_historical_execution_events AS previous
                WHERE previous.execution_id=NEW.execution_id
                  AND previous.sequence=NEW.sequence-1
                  AND previous.event_digest=NEW.previous_event_digest
            )
        )
        BEGIN
            SELECT RAISE(ABORT,'historical_execution_event_chain_mismatch');
        END
        """,
        """
        CREATE TRIGGER ingestion_historical_approvals_exact_plan
        BEFORE INSERT ON ingestion_historical_approvals
        WHEN NOT EXISTS(
            SELECT 1
            FROM ingestion_historical_batches AS batch
            JOIN ingestion_historical_manifests AS manifest
              ON manifest.historical_manifest_id=batch.historical_manifest_id
            WHERE batch.historical_batch_id=NEW.historical_batch_id
              AND batch.historical_manifest_id=NEW.historical_manifest_id
              AND batch.plan_digest=NEW.plan_digest
              AND batch.target_snapshot_fingerprint=
                  NEW.target_snapshot_fingerprint
              AND manifest.evidence_policy_version=
                  NEW.evidence_policy_version
              AND manifest.evidence_digest=NEW.evidence_digest
        )
        BEGIN
            SELECT RAISE(ABORT,'historical_approval_exact_plan_mismatch');
        END
        """,
        """
        CREATE TRIGGER ingestion_historical_executions_exact_approval
        BEFORE INSERT ON ingestion_historical_executions
        WHEN NOT EXISTS(
            SELECT 1
            FROM ingestion_historical_approvals AS approval
            WHERE approval.historical_batch_id=NEW.historical_batch_id
              AND approval.approval_digest=NEW.approval_digest
              AND approval.approval_scope='exact_plan'
        )
        BEGIN
            SELECT RAISE(ABORT,'historical_execution_approval_mismatch');
        END
        """,
        """
        CREATE TRIGGER ingestion_historical_verifications_exact_execution
        BEFORE INSERT ON ingestion_historical_verifications
        WHEN NOT EXISTS(
            SELECT 1
            FROM ingestion_historical_executions AS execution
            JOIN ingestion_historical_approvals AS approval
              ON approval.approval_digest=execution.approval_digest
            WHERE execution.execution_id=NEW.execution_id
              AND execution.historical_batch_id=NEW.historical_batch_id
              AND approval.plan_digest=NEW.plan_digest
              AND approval.target_snapshot_fingerprint=
                  NEW.target_snapshot_fingerprint
              AND approval.evidence_policy_version=
                  NEW.evidence_policy_version
              AND approval.evidence_digest=NEW.evidence_digest
        )
        BEGIN
            SELECT RAISE(ABORT,'historical_verification_chain_mismatch');
        END
        """,
    ))
    return tuple(statements)


from memory.ingestion_historical_schema import (
    UNIFIED_V2_INDEX_DDL,
    UNIFIED_V2_TABLE_DDL,
    UNIFIED_V2_TABLES,
    UNIFIED_V2_TRIGGER_DDL,
)

# The pre-ruling historical-only builders above are deliberately inactive.
# v8302's authoritative payload is the unified v2 chain below; v8301 remains
# byte-for-byte frozen and readable for compatibility.
HISTORICAL_INGESTION_TABLES = UNIFIED_V2_TABLES
HISTORICAL_MIGRATION_TABLE_DDL = tuple(
    _normalize_sql(sql) for sql in UNIFIED_V2_TABLE_DDL
)
HISTORICAL_MIGRATION_INDEX_DDL = tuple(
    _normalize_sql(sql) for sql in UNIFIED_V2_INDEX_DDL
)
HISTORICAL_MIGRATION_TRIGGER_DDL = tuple(
    _normalize_sql(sql) for sql in UNIFIED_V2_TRIGGER_DDL
)
HISTORICAL_MIGRATION_DDL = (
    HISTORICAL_MIGRATION_TABLE_DDL
    + HISTORICAL_MIGRATION_INDEX_DDL
    + HISTORICAL_MIGRATION_TRIGGER_DDL
)
HISTORICAL_MIGRATION_DIGEST = _digest({
    "migration_id": HISTORICAL_MIGRATION_ID,
    "migration_version": HISTORICAL_MIGRATION_VERSION,
    "migration_name": HISTORICAL_MIGRATION_NAME,
    "requires_migration_version": MIGRATION_VERSION,
    "requires_migration_digest": MIGRATION_DIGEST,
    "statements": list(HISTORICAL_MIGRATION_DDL),
    "user_version": HISTORICAL_MIGRATION_VERSION,
})


@dataclass(frozen=True)
class HistoricalMigrationPlan:
    migration_id: str
    migration_version: int
    migration_name: str
    migration_digest: str
    required_migration_version: int
    required_migration_digest: str
    source_schema_fingerprint: str
    expected_target_schema_fingerprint: str
    statement_digests: tuple[str, ...]
    table_count: int
    index_count: int
    trigger_count: int
    destructive_statement_count: int
    estimated_size_growth_bytes: int
    plan_schema_version: str = HISTORICAL_PLAN_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_schema_version": self.plan_schema_version,
            "migration_id": self.migration_id,
            "migration_version": self.migration_version,
            "migration_name": self.migration_name,
            "migration_digest": self.migration_digest,
            "required_migration_version": self.required_migration_version,
            "required_migration_digest": self.required_migration_digest,
            "source_schema_fingerprint": self.source_schema_fingerprint,
            "expected_target_schema_fingerprint":
                self.expected_target_schema_fingerprint,
            "statement_digests": list(self.statement_digests),
            "objects_added": {
                "tables": self.table_count,
                "indexes": self.index_count,
                "triggers": self.trigger_count,
            },
            "destructive_statement_count": self.destructive_statement_count,
            "estimated_size_growth_bytes": self.estimated_size_growth_bytes,
        }


@dataclass(frozen=True)
class HistoricalMigrationResult:
    status: str
    migration_id: str
    migration_version: int
    migration_digest: str
    v8301_schema_fingerprint: str
    before_schema_fingerprint: str
    after_schema_fingerprint: str
    integrity_check: str
    foreign_key_violation_count: int
    recovery_required: bool
    result_schema_version: str = HISTORICAL_RESULT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "result_schema_version": self.result_schema_version,
            "status": self.status,
            "migration_id": self.migration_id,
            "migration_version": self.migration_version,
            "migration_digest": self.migration_digest,
            "v8301_schema_fingerprint": self.v8301_schema_fingerprint,
            "before_schema_fingerprint": self.before_schema_fingerprint,
            "after_schema_fingerprint": self.after_schema_fingerprint,
            "integrity_check": self.integrity_check,
            "foreign_key_violation_count": self.foreign_key_violation_count,
            "recovery_required": self.recovery_required,
        }


def _expected_historical_target_fingerprint(snapshot: SchemaSnapshot) -> str:
    conn = _recreate_schema(snapshot)
    try:
        for statement in HISTORICAL_MIGRATION_DDL:
            conn.execute(statement)
        conn.execute(f"PRAGMA user_version={HISTORICAL_MIGRATION_VERSION}")
        return schema_fingerprint(conn)
    except sqlite3.Error as exc:
        raise MigrationRehearsalError(
            "historical_target_schema_not_recreatable"
        ) from exc
    finally:
        conn.close()


def _historical_marker_consistent(snapshot: SchemaSnapshot) -> bool:
    return (
        snapshot.marker_state == "marked"
        and snapshot.migration_version == HISTORICAL_MIGRATION_VERSION
        and snapshot.migration_digest == HISTORICAL_MIGRATION_DIGEST
        and snapshot.marker_resulting_fingerprint == snapshot.schema_fingerprint
        and set(HISTORICAL_INGESTION_TABLES).issubset(snapshot.object_names)
        and set(REQUIRED_TABLES).issubset(snapshot.object_names)
        and snapshot.pragmas["user_version"] == HISTORICAL_MIGRATION_VERSION
    )


def inspect_v8301_schema(
    db_path: str | os.PathLike[str],
) -> SchemaSnapshot:
    snapshot = inspect_schema(db_path)
    if not _marker_consistent(snapshot):
        if (
            snapshot.marker_state == "marked"
            and snapshot.migration_version == MIGRATION_VERSION
            and snapshot.migration_digest != MIGRATION_DIGEST
        ):
            raise MigrationRehearsalError("v8301_digest_mismatch")
        raise MigrationRehearsalError("historical_migration_requires_v8301")
    return snapshot


def build_historical_migration_plan(
    snapshot: SchemaSnapshot,
) -> HistoricalMigrationPlan:
    if not isinstance(snapshot, SchemaSnapshot):
        raise MigrationRehearsalError("invalid_historical_schema_snapshot")
    if _historical_marker_consistent(snapshot):
        return HistoricalMigrationPlan(
            migration_id=HISTORICAL_MIGRATION_ID,
            migration_version=HISTORICAL_MIGRATION_VERSION,
            migration_name=HISTORICAL_MIGRATION_NAME,
            migration_digest=HISTORICAL_MIGRATION_DIGEST,
            required_migration_version=MIGRATION_VERSION,
            required_migration_digest=MIGRATION_DIGEST,
            source_schema_fingerprint=snapshot.schema_fingerprint,
            expected_target_schema_fingerprint=snapshot.schema_fingerprint,
            statement_digests=tuple(
                _digest(sql) for sql in HISTORICAL_MIGRATION_DDL
            ),
            table_count=len(HISTORICAL_MIGRATION_TABLE_DDL),
            index_count=len(HISTORICAL_MIGRATION_INDEX_DDL),
            trigger_count=len(HISTORICAL_MIGRATION_TRIGGER_DDL),
            destructive_statement_count=0,
            estimated_size_growth_bytes=0,
        )
    if (
        snapshot.marker_state == "marked"
        and snapshot.migration_version is not None
        and snapshot.migration_version > HISTORICAL_MIGRATION_VERSION
    ):
        raise MigrationRehearsalError("higher_unknown_schema_version")
    if (
        snapshot.marker_state == "marked"
        and snapshot.migration_version == HISTORICAL_MIGRATION_VERSION
    ):
        raise MigrationRehearsalError(
            "historical_migration_digest_mismatch",
            recovery_required=True,
        )
    if snapshot.marker_state != "marked":
        raise MigrationRehearsalError("historical_migration_requires_v8301")
    if snapshot.migration_version != MIGRATION_VERSION:
        raise MigrationRehearsalError("unknown_migration_history")
    if snapshot.migration_digest != MIGRATION_DIGEST:
        raise MigrationRehearsalError("v8301_digest_mismatch")
    if snapshot.marker_resulting_fingerprint != snapshot.schema_fingerprint:
        raise MigrationRehearsalError(
            "v8301_schema_drift", recovery_required=True,
        )
    if (
        snapshot.pragmas["user_version"] != MIGRATION_VERSION
        or not set(REQUIRED_TABLES).issubset(snapshot.object_names)
    ):
        raise MigrationRehearsalError(
            "v8301_schema_incomplete", recovery_required=True,
        )
    collision = set(HISTORICAL_INGESTION_TABLES).intersection(
        snapshot.object_names
    )
    if collision:
        raise MigrationRehearsalError(
            "historical_migration_marker_missing",
            recovery_required=True,
        )
    return HistoricalMigrationPlan(
        migration_id=HISTORICAL_MIGRATION_ID,
        migration_version=HISTORICAL_MIGRATION_VERSION,
        migration_name=HISTORICAL_MIGRATION_NAME,
        migration_digest=HISTORICAL_MIGRATION_DIGEST,
        required_migration_version=MIGRATION_VERSION,
        required_migration_digest=MIGRATION_DIGEST,
        source_schema_fingerprint=snapshot.schema_fingerprint,
        expected_target_schema_fingerprint=
            _expected_historical_target_fingerprint(snapshot),
        statement_digests=tuple(
            _digest(sql) for sql in HISTORICAL_MIGRATION_DDL
        ),
        table_count=len(HISTORICAL_MIGRATION_TABLE_DDL),
        index_count=len(HISTORICAL_MIGRATION_INDEX_DDL),
        trigger_count=len(HISTORICAL_MIGRATION_TRIGGER_DDL),
        destructive_statement_count=0,
        estimated_size_growth_bytes=512 * 1024,
    )


def validate_historical_migration_plan(
    plan: HistoricalMigrationPlan,
) -> None:
    if not isinstance(plan, HistoricalMigrationPlan):
        raise MigrationRehearsalError("invalid_historical_migration_plan")
    if (
        plan.migration_id != HISTORICAL_MIGRATION_ID
        or plan.migration_version != HISTORICAL_MIGRATION_VERSION
        or plan.migration_name != HISTORICAL_MIGRATION_NAME
        or plan.migration_digest != HISTORICAL_MIGRATION_DIGEST
        or plan.required_migration_version != MIGRATION_VERSION
        or plan.required_migration_digest != MIGRATION_DIGEST
        or plan.plan_schema_version != HISTORICAL_PLAN_SCHEMA_VERSION
    ):
        raise MigrationRehearsalError(
            "historical_migration_plan_digest_mismatch"
        )
    if plan.statement_digests != tuple(
        _digest(sql) for sql in HISTORICAL_MIGRATION_DDL
    ):
        raise MigrationRehearsalError(
            "historical_migration_plan_statement_mismatch"
        )
    if plan.destructive_statement_count != 0:
        raise MigrationRehearsalError("destructive_migration_forbidden")
    for statement in HISTORICAL_MIGRATION_DDL:
        if re.search(
            r"^\s*(DROP|ALTER|DELETE|UPDATE)\b", statement, re.IGNORECASE,
        ):
            raise MigrationRehearsalError("destructive_migration_forbidden")


def _historical_lock_path(target: Path) -> Path:
    return target.with_name(
        "."
        + hashlib.sha256(str(target).encode("utf-8")).hexdigest()
        + ".v83g0.lock"
    )


def _acquire_historical_lock(target: Path) -> tuple[int, Path]:
    lock = _historical_lock_path(target)
    try:
        descriptor = os.open(
            lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600,
        )
    except FileExistsError as exc:
        raise MigrationRehearsalError(
            "concurrent_historical_migrator_blocked"
        ) from exc
    os.write(descriptor, b"v8.3-g0")
    return descriptor, lock


def apply_historical_migration_to_clone(
    plan: HistoricalMigrationPlan,
    clone_path: str | os.PathLike[str],
    *,
    rehearsal_id: str,
    fault_injector: FaultInjector | None = None,
    minimum_free_bytes: int = 2 * 1024 * 1024,
) -> HistoricalMigrationResult:
    """Apply v8302 to a disposable, correctly marked v8301 clone only."""
    validate_historical_migration_plan(plan)
    _identity(rehearsal_id, "invalid_rehearsal_id")
    target = validate_clone_database_path(clone_path)
    if shutil.disk_usage(target.parent).free < minimum_free_bytes:
        raise MigrationRehearsalError("insufficient_disk_space")
    descriptor, lock = _acquire_historical_lock(target)
    committed = False
    before: SchemaSnapshot | None = None
    try:
        conn = sqlite3.connect(target, timeout=5.0, isolation_level=None)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        before = inspect_schema_connection(conn)
        if _historical_marker_consistent(before):
            if (
                before.schema_fingerprint
                != plan.expected_target_schema_fingerprint
            ):
                raise MigrationRehearsalError(
                    "historical_migrated_schema_drift",
                    recovery_required=True,
                )
            return HistoricalMigrationResult(
                status="already_applied",
                migration_id=HISTORICAL_MIGRATION_ID,
                migration_version=HISTORICAL_MIGRATION_VERSION,
                migration_digest=HISTORICAL_MIGRATION_DIGEST,
                v8301_schema_fingerprint=before.schema_fingerprint,
                before_schema_fingerprint=before.schema_fingerprint,
                after_schema_fingerprint=before.schema_fingerprint,
                integrity_check="ok",
                foreign_key_violation_count=0,
                recovery_required=False,
            )
        if (
            before.marker_state == "marked"
            and before.migration_version == HISTORICAL_MIGRATION_VERSION
        ):
            raise MigrationRehearsalError(
                "historical_migration_digest_mismatch",
                recovery_required=True,
            )
        if not _marker_consistent(before):
            if (
                before.marker_state == "marked"
                and before.migration_version == MIGRATION_VERSION
                and before.migration_digest != MIGRATION_DIGEST
            ):
                raise MigrationRehearsalError("v8301_digest_mismatch")
            raise MigrationRehearsalError(
                "historical_migration_requires_v8301"
            )
        if before.schema_fingerprint != plan.source_schema_fingerprint:
            raise MigrationRehearsalError(
                "historical_migration_source_schema_drift"
            )
        if set(HISTORICAL_INGESTION_TABLES).intersection(
            before.object_names
        ):
            raise MigrationRehearsalError(
                "historical_migration_marker_missing",
                recovery_required=True,
            )

        conn.execute("BEGIN IMMEDIATE")
        try:
            for index, statement in enumerate(HISTORICAL_MIGRATION_TABLE_DDL):
                conn.execute(statement)
                if index == 2:
                    _checkpoint(
                        "historical_partial_tables_created",
                        conn,
                        fault_injector,
                    )
            _checkpoint("historical_tables_created", conn, fault_injector)
            for statement in HISTORICAL_MIGRATION_INDEX_DDL:
                conn.execute(statement)
            _checkpoint("historical_indexes_created", conn, fault_injector)
            for statement in HISTORICAL_MIGRATION_TRIGGER_DDL:
                conn.execute(statement)
            _checkpoint("historical_triggers_created", conn, fault_injector)
            conn.execute(f"PRAGMA user_version={HISTORICAL_MIGRATION_VERSION}")
            target_fingerprint = schema_fingerprint(conn)
            if (
                target_fingerprint
                != plan.expected_target_schema_fingerprint
            ):
                raise MigrationRehearsalError(
                    "historical_target_schema_fingerprint_mismatch"
                )
            applied_at = utc_now()
            conn.execute(
                "INSERT INTO schema_migrations VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    HISTORICAL_MIGRATION_ID,
                    HISTORICAL_MIGRATION_VERSION,
                    HISTORICAL_MIGRATION_NAME,
                    HISTORICAL_MIGRATION_DIGEST,
                    applied_at,
                    HISTORICAL_APPLICATION_VERSION,
                    before.schema_fingerprint,
                    target_fingerprint,
                    rehearsal_id,
                    HISTORICAL_MIGRATION_SCHEMA_VERSION,
                ),
            )
            meta_value = _canonical_text({
                "application_version": HISTORICAL_APPLICATION_VERSION,
                "migration_digest": HISTORICAL_MIGRATION_DIGEST,
                "migration_version": HISTORICAL_MIGRATION_VERSION,
                "v8301_migration_digest": MIGRATION_DIGEST,
                "v8301_schema_fingerprint": before.schema_fingerprint,
                "v8302_schema_fingerprint": target_fingerprint,
            })
            conn.execute(
                "INSERT INTO yunos_schema_meta VALUES(?,?,?,?,?)",
                (
                    "historical_ingestion_schema",
                    meta_value,
                    hashlib.sha256(meta_value.encode("utf-8")).hexdigest(),
                    applied_at,
                    HISTORICAL_MIGRATION_SCHEMA_VERSION,
                ),
            )
            _checkpoint("historical_marker_written", conn, fault_injector)
            _checkpoint("historical_before_commit", conn, fault_injector)
            conn.execute("COMMIT")
            committed = True
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

        _checkpoint("historical_after_commit_report", conn, fault_injector)
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        _checkpoint("historical_integrity_checked", conn, fault_injector)
        foreign_key_violations = list(conn.execute("PRAGMA foreign_key_check"))
        _checkpoint("historical_foreign_keys_checked", conn, fault_injector)
        after = inspect_schema_connection(conn)
        if integrity != "ok":
            raise MigrationRehearsalError(
                "historical_integrity_check_failed",
                recovery_required=True,
            )
        if foreign_key_violations:
            raise MigrationRehearsalError(
                "historical_foreign_key_check_failed",
                recovery_required=True,
            )
        if (
            not _historical_marker_consistent(after)
            or after.schema_fingerprint != target_fingerprint
        ):
            raise MigrationRehearsalError(
                "historical_post_commit_schema_verification_failed",
                recovery_required=True,
            )
        return HistoricalMigrationResult(
            status="completed",
            migration_id=HISTORICAL_MIGRATION_ID,
            migration_version=HISTORICAL_MIGRATION_VERSION,
            migration_digest=HISTORICAL_MIGRATION_DIGEST,
            v8301_schema_fingerprint=before.schema_fingerprint,
            before_schema_fingerprint=before.schema_fingerprint,
            after_schema_fingerprint=after.schema_fingerprint,
            integrity_check=integrity,
            foreign_key_violation_count=0,
            recovery_required=False,
        )
    except MigrationRehearsalError:
        raise
    except MigrationInterrupted:
        raise
    except sqlite3.Error as exc:
        raise MigrationRehearsalError(
            "historical_migration_sql_failed",
            recovery_required=committed,
        ) from exc
    except OSError as exc:
        raise MigrationRehearsalError(
            "historical_migration_io_failed",
            recovery_required=committed,
        ) from exc
    finally:
        if "conn" in locals():
            conn.close()
        _release_lock(descriptor, lock)


def verify_historical_migrated_schema(
    db_path: str | os.PathLike[str],
    *,
    expected_fingerprint: str | None = None,
    require_empty: bool = False,
) -> dict[str, Any]:
    target = validate_clone_database_path(db_path)
    with closing(sqlite3.connect(target)) as conn:
        conn.execute("PRAGMA query_only=ON")
        snapshot = inspect_schema_connection(conn)
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_key_violations = list(conn.execute("PRAGMA foreign_key_check"))
        new_counts = {
            table: int(
                conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            )
            for table in HISTORICAL_INGESTION_TABLES
        }
        marker_rows = conn.execute(
            "SELECT migration_version,migration_digest,"
            "previous_schema_fingerprint,resulting_schema_fingerprint "
            "FROM schema_migrations "
            "WHERE migration_version IN (?,?) ORDER BY migration_version",
            (MIGRATION_VERSION, HISTORICAL_MIGRATION_VERSION),
        ).fetchall()
        meta_row = conn.execute(
            "SELECT meta_value,value_digest FROM yunos_schema_meta "
            "WHERE meta_key=?",
            ("historical_ingestion_schema",),
        ).fetchone()
    if not _historical_marker_consistent(snapshot):
        raise MigrationRehearsalError(
            "historical_migrated_schema_marker_mismatch",
            recovery_required=True,
        )
    if expected_fingerprint and snapshot.schema_fingerprint != expected_fingerprint:
        raise MigrationRehearsalError(
            "historical_migrated_schema_drift",
            recovery_required=True,
        )
    if integrity != "ok":
        raise MigrationRehearsalError(
            "historical_integrity_check_failed",
            recovery_required=True,
        )
    if foreign_key_violations:
        raise MigrationRehearsalError(
            "historical_foreign_key_check_failed",
            recovery_required=True,
        )
    if (
        len(marker_rows) != 2
        or marker_rows[0][0] != MIGRATION_VERSION
        or marker_rows[0][1] != MIGRATION_DIGEST
        or marker_rows[1][0] != HISTORICAL_MIGRATION_VERSION
        or marker_rows[1][1] != HISTORICAL_MIGRATION_DIGEST
        or marker_rows[0][3] != marker_rows[1][2]
        or marker_rows[1][3] != snapshot.schema_fingerprint
    ):
        raise MigrationRehearsalError(
            "historical_marker_chain_mismatch",
            recovery_required=True,
        )
    if meta_row is None or hashlib.sha256(
        str(meta_row[0]).encode("utf-8")
    ).hexdigest() != meta_row[1]:
        raise MigrationRehearsalError(
            "historical_schema_meta_mismatch",
            recovery_required=True,
        )
    if require_empty and any(new_counts.values()):
        raise MigrationRehearsalError(
            "historical_tables_not_empty",
            recovery_required=True,
        )
    return {
        "schema_fingerprint": snapshot.schema_fingerprint,
        "v8301_schema_fingerprint": marker_rows[0][3],
        "v8302_schema_fingerprint": marker_rows[1][3],
        "migration_versions": [row[0] for row in marker_rows],
        "integrity_check": integrity,
        "foreign_key_violation_count": len(foreign_key_violations),
        "required_table_count": len(HISTORICAL_INGESTION_TABLES),
        "historical_table_row_counts": new_counts,
        "marker_chain_valid": True,
        "schema_meta_valid": True,
    }


def inject_historical_migration_failure(
    checkpoint: str,
    *,
    crash: bool = False,
) -> FaultInjector:
    allowed = {
        "historical_partial_tables_created",
        "historical_tables_created",
        "historical_indexes_created",
        "historical_triggers_created",
        "historical_marker_written",
        "historical_before_commit",
        "historical_after_commit_report",
        "historical_integrity_checked",
        "historical_foreign_keys_checked",
    }
    if checkpoint not in allowed:
        raise MigrationRehearsalError("unknown_historical_failure_checkpoint")

    def injector(name: str, _conn: sqlite3.Connection) -> None:
        if name == checkpoint:
            if crash:
                raise MigrationInterrupted(name)
            raise MigrationRehearsalError(
                f"injected_{name}_failure",
                recovery_required=name in {
                    "historical_after_commit_report",
                    "historical_integrity_checked",
                    "historical_foreign_keys_checked",
                },
            )

    return injector


def assess_historical_migration_recovery(
    clone_path: str | os.PathLike[str],
    *,
    plan: HistoricalMigrationPlan | None = None,
) -> dict[str, Any]:
    snapshot = inspect_schema(validate_clone_database_path(clone_path))
    if _historical_marker_consistent(snapshot):
        fingerprint_matches = (
            plan is None
            or snapshot.schema_fingerprint
            == plan.expected_target_schema_fingerprint
        )
        status = "completed" if fingerprint_matches else "recovery_required"
    elif _marker_consistent(snapshot):
        status = "not_applied"
    else:
        status = "recovery_required"
    return {
        "status": status,
        "manual_action": (
            "no_recovery_required"
            if status == "completed"
            else (
                "retry_only_after_operator_review"
                if status == "not_applied"
                else "stop_and_restore_verified_backup"
            )
        ),
        "automatic_retry": False,
        "schema_fingerprint": snapshot.schema_fingerprint,
        "marker_state": snapshot.marker_state,
        "migration_version": snapshot.migration_version,
    }


def apply_production_historical_migration(
    *_args: Any,
    **_kwargs: Any,
) -> None:
    raise MigrationRehearsalError("production_migration_not_authorized")
