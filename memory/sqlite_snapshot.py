"""Deterministic, read-only SQLite snapshots for production audit preflight.

The snapshot protocol preserves SQLite storage classes and exact TEXT/BLOB
bytes.  It never initializes schema, opens a writable production connection,
or participates in an authorization capability.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import struct
from typing import Any, Sequence


SNAPSHOT_PROTOCOL_VERSION = "v8.0-d2.2-deterministic-sqlite-snapshot-1"
VALUE_ENCODING_VERSION = "v8.0-d2.2-sqlite-value-1"
ROW_ENCODING_VERSION = "v8.0-d2.2-sqlite-row-1"
TABLE_ENCODING_VERSION = "v8.0-d2.2-sqlite-table-1"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_PROFILE_SIDECAR = PROJECT_ROOT / "data" / "profile" / "approved_profile.db"
PRODUCTION_PROFILE_SNAPSHOT_TABLES = (
    "profile_enablement_schema",
    "direct_profile_proposals",
    "direct_profile_proposal_receipts",
    "profile_enablement_decisions",
    "profile_enablement_receipts",
    "d2_authorization_schema",
    "d2_authorization_manifests",
    "d2_authorization_results",
    "d2_authorization_batch_receipts",
)

_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_SQLITE_TYPES = {"null", "integer", "real", "text", "blob"}
_INTEGER_MIN = -(2**63)
_INTEGER_MAX = 2**63 - 1


class SQLiteSnapshotError(RuntimeError):
    """Raised when a deterministic snapshot cannot be proven safe and stable."""


def canonical_snapshot_bytes(value: Any) -> bytes:
    """Return canonical ASCII JSON bytes, rejecting non-finite JSON values."""
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii", errors="strict")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise SQLiteSnapshotError("snapshot value is not canonical ASCII JSON") from exc
    return encoded


def canonical_sqlite_value(
    value: Any, *, sqlite_type: str | None = None,
) -> dict[str, Any]:
    """Encode one SQLite value without losing its storage class or bytes."""
    inferred: str
    if value is None:
        inferred = "null"
        encoded: dict[str, Any] = {
            "encoding_version": VALUE_ENCODING_VERSION,
            "sqlite_type": inferred,
            "value": None,
        }
    elif isinstance(value, bool):
        raise SQLiteSnapshotError("boolean is not an unambiguous SQLite INTEGER value")
    elif isinstance(value, int):
        if not _INTEGER_MIN <= value <= _INTEGER_MAX:
            raise SQLiteSnapshotError("SQLite INTEGER is outside signed 64-bit range")
        inferred = "integer"
        encoded = {
            "encoding_version": VALUE_ENCODING_VERSION,
            "sqlite_type": inferred,
            "decimal": str(value),
        }
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise SQLiteSnapshotError("non-finite SQLite REAL is not snapshot-safe")
        inferred = "real"
        encoded = {
            "encoding_version": VALUE_ENCODING_VERSION,
            "sqlite_type": inferred,
            "ieee754_binary64_hex": struct.pack(">d", value).hex(),
        }
    elif isinstance(value, str):
        try:
            raw = value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise SQLiteSnapshotError("SQLite TEXT is not strict UTF-8 encodable") from exc
        inferred = "text"
        encoded = {
            "encoding_version": VALUE_ENCODING_VERSION,
            "sqlite_type": inferred,
            "utf8_base64": base64.b64encode(raw).decode("ascii"),
            "utf8_sha256": hashlib.sha256(raw).hexdigest(),
            "code_points": [ord(char) for char in value],
        }
    elif isinstance(value, (bytes, bytearray, memoryview)):
        raw = value.tobytes() if isinstance(value, memoryview) else bytes(value)
        inferred = "blob"
        encoded = {
            "encoding_version": VALUE_ENCODING_VERSION,
            "sqlite_type": inferred,
            "base64": base64.b64encode(raw).decode("ascii"),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "length": len(raw),
        }
    else:
        raise SQLiteSnapshotError(f"unsupported SQLite value type: {type(value).__name__}")

    if sqlite_type is not None:
        if sqlite_type not in _SQLITE_TYPES:
            raise SQLiteSnapshotError("SQLite typeof() returned an unsupported storage class")
        if sqlite_type != inferred:
            raise SQLiteSnapshotError("SQLite storage class and Python value type disagree")
    return encoded


def _identity(prefix: str, payload: Any) -> str:
    return prefix + hashlib.sha256(canonical_snapshot_bytes(payload)).hexdigest()


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise SQLiteSnapshotError("SQLite identifier is outside the strict ASCII policy")
    return value


def _quoted_identifier(value: Any) -> str:
    return f'"{_identifier(value)}"'


def _int_pragma(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute(f"PRAGMA {_identifier(name)}").fetchone()
    if row is None or isinstance(row[0], bool) or not isinstance(row[0], int):
        raise SQLiteSnapshotError(f"SQLite PRAGMA {name} is not an integer")
    return row[0]


def _table_inventory(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute(
        "SELECT name,sql FROM sqlite_schema "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    inventory: dict[str, str] = {}
    for name, create_sql in rows:
        table_name = _identifier(name)
        if not isinstance(create_sql, str):
            raise SQLiteSnapshotError("table schema SQL is unavailable")
        inventory[table_name] = create_sql
    return inventory


def _selected_tables(
    inventory: dict[str, str], table_names: Sequence[str] | None,
) -> list[str]:
    if table_names is None:
        return sorted(inventory)
    selected = [_identifier(name) for name in table_names]
    if len(selected) != len(set(selected)):
        raise SQLiteSnapshotError("snapshot table list contains duplicates")
    missing = sorted(set(selected) - set(inventory))
    if missing:
        raise SQLiteSnapshotError("snapshot table is absent from fixed schema introspection")
    return sorted(selected)


def _table_columns(conn: sqlite3.Connection, table_name: str) -> list[dict[str, Any]]:
    rows = conn.execute(f"PRAGMA table_xinfo({_quoted_identifier(table_name)})").fetchall()
    if not rows:
        raise SQLiteSnapshotError("snapshot table has no introspectable columns")
    columns: list[dict[str, Any]] = []
    expected_cid = 0
    for row in rows:
        cid, name, declared_type, not_null, default_sql, pk_ordinal, hidden = row
        if cid != expected_cid:
            raise SQLiteSnapshotError("snapshot column order is not contiguous")
        expected_cid += 1
        if hidden != 0:
            raise SQLiteSnapshotError("hidden or generated columns are not supported")
        column_name = _identifier(name)
        if not isinstance(declared_type, str):
            raise SQLiteSnapshotError("declared SQLite column type is invalid")
        if isinstance(not_null, bool) or not isinstance(not_null, int) or not_null not in {0, 1}:
            raise SQLiteSnapshotError("SQLite not-null metadata is invalid")
        if isinstance(pk_ordinal, bool) or not isinstance(pk_ordinal, int) or pk_ordinal < 0:
            raise SQLiteSnapshotError("SQLite primary-key metadata is invalid")
        columns.append({
            "cid": str(cid),
            "name": column_name,
            "declared_type": declared_type,
            "not_null": bool(not_null),
            "default_sql": canonical_sqlite_value(
                default_sql, sqlite_type="null" if default_sql is None else "text"
            ),
            "primary_key_ordinal": str(pk_ordinal),
        })
    return columns


def _rowid_identifier(column_names: set[str]) -> str:
    for candidate in ("rowid", "_rowid_", "oid"):
        if candidate not in column_names:
            return candidate
    raise SQLiteSnapshotError("table shadows every SQLite rowid identifier")


def _table_rows(
    conn: sqlite3.Connection,
    table_name: str,
    columns: list[dict[str, Any]],
    create_sql: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    primary_key = sorted(
        (column for column in columns if int(column["primary_key_ordinal"])),
        key=lambda column: int(column["primary_key_ordinal"]),
    )
    primary_key_names = {column["name"] for column in primary_key}
    column_names = [column["name"] for column in columns]
    quoted_columns = [_quoted_identifier(name) for name in column_names]
    expressions: list[str] = []
    for quoted in quoted_columns:
        expressions.extend((quoted, f"typeof({quoted})"))

    rowid_name: str | None = None
    if primary_key:
        ordering = {
            "kind": "primary_key",
            "columns": [column["name"] for column in primary_key],
        }
    else:
        if "WITHOUT ROWID" in create_sql.upper():
            raise SQLiteSnapshotError("WITHOUT ROWID table has no stable primary key")
        rowid_name = _rowid_identifier(set(column_names))
        expressions.insert(0, _quoted_identifier(rowid_name))
        ordering = {"kind": "rowid", "columns": [rowid_name]}

    query = f"SELECT {','.join(expressions)} FROM {_quoted_identifier(table_name)}"
    rows: list[dict[str, Any]] = []
    seen_order_keys: set[bytes] = set()
    for raw_row in conn.execute(query).fetchall():
        offset = 0
        if rowid_name is not None:
            rowid_value = canonical_sqlite_value(raw_row[0], sqlite_type="integer")
            order_key = {
                "kind": "rowid",
                "values": [{"column": rowid_name, "value": rowid_value}],
            }
            offset = 1
        else:
            order_key = None

        cells: list[dict[str, Any]] = []
        by_name: dict[str, dict[str, Any]] = {}
        for index, column_name in enumerate(column_names):
            value = raw_row[offset + index * 2]
            storage_class = raw_row[offset + index * 2 + 1]
            if not isinstance(storage_class, str):
                raise SQLiteSnapshotError("SQLite typeof() did not return text")
            if column_name in primary_key_names and storage_class == "null":
                raise SQLiteSnapshotError(
                    "SQLite primary-key component is NULL and cannot order rows"
                )
            encoded = canonical_sqlite_value(value, sqlite_type=storage_class)
            cell = {"column": column_name, "value": encoded}
            cells.append(cell)
            by_name[column_name] = encoded
        if primary_key:
            order_key = {
                "kind": "primary_key",
                "values": [
                    {"column": column["name"], "value": by_name[column["name"]]}
                    for column in primary_key
                ],
            }
        if order_key is None:
            raise SQLiteSnapshotError("stable row ordering key is unavailable")

        canonical_order_key = canonical_snapshot_bytes(order_key)
        if canonical_order_key in seen_order_keys:
            raise SQLiteSnapshotError("stable row ordering key is not unique")
        seen_order_keys.add(canonical_order_key)
        row_payload = {
            "encoding_version": ROW_ENCODING_VERSION,
            "table_name": table_name,
            "order_key": order_key,
            "cells": cells,
        }
        rows.append({**row_payload, "row_identity": _identity("srow_", row_payload)})

    rows.sort(key=lambda row: canonical_snapshot_bytes(row["order_key"]))
    return ordering, rows


def snapshot_sqlite_connection(
    conn: sqlite3.Connection, *, table_names: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Snapshot selected introspected tables using SELECT/PRAGMA operations only."""
    inventory = _table_inventory(conn)
    selected = _selected_tables(inventory, table_names)
    tables: list[dict[str, Any]] = []
    for table_name in selected:
        columns = _table_columns(conn, table_name)
        create_sql = inventory[table_name]
        ordering, rows = _table_rows(conn, table_name, columns, create_sql)
        schema_payload = {
            "encoding_version": TABLE_ENCODING_VERSION,
            "table_name": table_name,
            "create_sql": canonical_sqlite_value(create_sql, sqlite_type="text"),
            "columns": columns,
            "ordering": ordering,
        }
        table_payload = {
            **schema_payload,
            "schema_identity": _identity("ssch_", schema_payload),
            "rows": rows,
        }
        tables.append({**table_payload, "table_identity": _identity("stab_", table_payload)})

    schema_version = _int_pragma(conn, "schema_version")
    user_version = _int_pragma(conn, "user_version")
    application_id = _int_pragma(conn, "application_id")
    encoding_row = conn.execute("PRAGMA encoding").fetchone()
    if encoding_row is None or not isinstance(encoding_row[0], str):
        raise SQLiteSnapshotError("SQLite encoding metadata is invalid")
    schema_payload = {
        "snapshot_protocol_version": SNAPSHOT_PROTOCOL_VERSION,
        "sqlite_schema_version": str(schema_version),
        "sqlite_user_version": str(user_version),
        "sqlite_application_id": str(application_id),
        "sqlite_encoding": encoding_row[0],
        "tables": [
            {"table_name": table["table_name"], "schema_identity": table["schema_identity"]}
            for table in tables
        ],
    }
    snapshot_payload = {
        **schema_payload,
        "database_schema_identity": _identity("sdbs_", schema_payload),
        "tables": tables,
    }
    return {
        **snapshot_payload,
        "snapshot_identity": _identity("sdb_", snapshot_payload),
    }


def snapshot_sqlite_database(
    db_path: str | Path, *, table_names: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Open a database immutable/read-only and return its deterministic snapshot."""
    try:
        target = Path(os.path.realpath(Path(db_path).expanduser())).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SQLiteSnapshotError("snapshot database path cannot be resolved") from exc
    if not target.is_file():
        raise SQLiteSnapshotError("snapshot target is not a regular database file")
    for suffix in ("-journal", "-wal"):
        if Path(str(target) + suffix).exists():
            raise SQLiteSnapshotError("snapshot target has an unresolved SQLite journal")
    try:
        conn = sqlite3.connect(target.as_uri() + "?mode=ro&immutable=1", uri=True)
    except sqlite3.Error as exc:
        raise SQLiteSnapshotError("snapshot database cannot be opened read-only") from exc
    try:
        conn.execute("PRAGMA query_only=ON")
        rows = conn.execute("PRAGMA database_list").fetchall()
        main_rows = [row for row in rows if row[1] == "main"]
        if len(main_rows) != 1 or not main_rows[0][2]:
            raise SQLiteSnapshotError("SQLite main database identity is unavailable")
        try:
            if not os.path.samefile(target, Path(main_rows[0][2])):
                raise SQLiteSnapshotError("SQLite opened a different snapshot target")
        except OSError as exc:
            raise SQLiteSnapshotError("SQLite snapshot file identity cannot be verified") from exc
        return snapshot_sqlite_connection(conn, table_names=table_names)
    except sqlite3.Error as exc:
        raise SQLiteSnapshotError("SQLite snapshot query failed") from exc
    finally:
        conn.close()


def snapshot_production_profile_authorization_database() -> dict[str, Any]:
    """Snapshot the fixed production profile sidecar; accepts no path or policy input."""
    return snapshot_sqlite_database(
        PRODUCTION_PROFILE_SIDECAR,
        table_names=PRODUCTION_PROFILE_SNAPSHOT_TABLES,
    )


def verify_preexisting_rows_unchanged(
    before: dict[str, Any], after: dict[str, Any],
) -> dict[str, Any]:
    """Prove schema identity is fixed and every old row remains byte-for-byte."""
    if before.get("snapshot_protocol_version") != SNAPSHOT_PROTOCOL_VERSION:
        raise SQLiteSnapshotError("before snapshot protocol mismatch")
    if after.get("snapshot_protocol_version") != SNAPSHOT_PROTOCOL_VERSION:
        raise SQLiteSnapshotError("after snapshot protocol mismatch")
    before_schema_identity = before.get("database_schema_identity")
    after_schema_identity = after.get("database_schema_identity")
    if (
        not isinstance(before_schema_identity, str)
        or not isinstance(after_schema_identity, str)
        or before_schema_identity != after_schema_identity
    ):
        raise SQLiteSnapshotError("SQLite database schema identity changed")

    def table_inventory(snapshot: dict[str, Any], label: str) -> dict[str, dict[str, Any]]:
        tables = snapshot.get("tables")
        if not isinstance(tables, list):
            raise SQLiteSnapshotError(f"{label} snapshot table inventory is invalid")
        inventory: dict[str, dict[str, Any]] = {}
        for table in tables:
            if not isinstance(table, dict):
                raise SQLiteSnapshotError(f"{label} snapshot table entry is invalid")
            table_name = _identifier(table.get("table_name"))
            if table_name in inventory:
                raise SQLiteSnapshotError(f"{label} snapshot table inventory has duplicates")
            inventory[table_name] = table
        return inventory

    before_tables = table_inventory(before, "before")
    after_tables = table_inventory(after, "after")
    if set(before_tables) != set(after_tables):
        raise SQLiteSnapshotError("SQLite snapshot table inventory changed")

    verified_rows = 0
    for table_name in sorted(before_tables):
        before_table = before_tables[table_name]
        after_table = after_tables[table_name]
        before_schema = {
            "table_name": before_table.get("table_name"),
            "encoding_version": before_table.get("encoding_version"),
            "create_sql": before_table.get("create_sql"),
            "columns": before_table.get("columns"),
            "ordering": before_table.get("ordering"),
            "schema_identity": before_table.get("schema_identity"),
        }
        after_schema = {
            "table_name": after_table.get("table_name"),
            "encoding_version": after_table.get("encoding_version"),
            "create_sql": after_table.get("create_sql"),
            "columns": after_table.get("columns"),
            "ordering": after_table.get("ordering"),
            "schema_identity": after_table.get("schema_identity"),
        }
        if canonical_snapshot_bytes(before_schema) != canonical_snapshot_bytes(after_schema):
            raise SQLiteSnapshotError("preexisting snapshot table schema changed")
        before_rows = before_table.get("rows")
        after_rows_list = after_table.get("rows")
        if not isinstance(before_rows, list) or not isinstance(after_rows_list, list):
            raise SQLiteSnapshotError("snapshot table rows are invalid")
        after_rows = {
            canonical_snapshot_bytes(row.get("order_key")): row
            for row in after_rows_list
            if isinstance(row, dict)
        }
        if len(after_rows) != len(after_rows_list):
            raise SQLiteSnapshotError("after snapshot contains duplicate ordering keys")
        for before_row in before_rows:
            if not isinstance(before_row, dict):
                raise SQLiteSnapshotError("before snapshot row is invalid")
            key = canonical_snapshot_bytes(before_row.get("order_key"))
            after_row = after_rows.get(key)
            if after_row is None:
                raise SQLiteSnapshotError("preexisting SQLite row was deleted")
            if before_row.get("row_identity") != after_row.get("row_identity"):
                raise SQLiteSnapshotError("preexisting SQLite row was modified")
            verified_rows += 1
    return {
        "snapshot_protocol_version": SNAPSHOT_PROTOCOL_VERSION,
        "preexisting_tables_verified": len(before_tables),
        "preexisting_rows_verified": verified_rows,
        "status": "preexisting_rows_unchanged",
    }


def snapshot_summary(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Return a privacy-safe identity/count summary without cell content."""
    return {
        "snapshot_protocol_version": snapshot.get("snapshot_protocol_version"),
        "snapshot_identity": snapshot.get("snapshot_identity"),
        "database_schema_identity": snapshot.get("database_schema_identity"),
        "tables": [
            {
                "table_name": table.get("table_name"),
                "schema_identity": table.get("schema_identity"),
                "table_identity": table.get("table_identity"),
                "row_count": len(table.get("rows", [])),
            }
            for table in snapshot.get("tables", [])
        ],
    }
