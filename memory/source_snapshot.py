"""Read-only, canonical snapshots of conversation message sources."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any


SNAPSHOT_SCHEMA_VERSION = 1


def connect_readonly(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path).expanduser().resolve()
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone())


def snapshots_for_database(
    db_path: str | Path,
    memory_space: str | None = None,
    conversation_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return source-only snapshots; the SQLite connection is always closed."""
    conn = connect_readonly(db_path)
    try:
        return snapshots_from_connection(conn, memory_space, conversation_id)
    finally:
        conn.close()


def snapshots_from_connection(
    conn: sqlite3.Connection,
    memory_space: str | None = None,
    conversation_id: str | None = None,
) -> list[dict[str, Any]]:
    if not table_exists(conn, "conversations") or not table_exists(conn, "messages"):
        return []
    conversation_columns = table_columns(conn, "conversations")
    message_columns = table_columns(conn, "messages")
    if "conversation_id" not in conversation_columns or "conversation_id" not in message_columns:
        return []

    has_conversation_space = "memory_space" in conversation_columns
    has_message_space = "memory_space" in message_columns
    where, params = [], []
    if conversation_id:
        where.append("c.conversation_id = ?")
        params.append(conversation_id)
    if memory_space and has_conversation_space:
        where.append("c.memory_space = ?")
        params.append(memory_space)
    rows = conn.execute(
        "SELECT c.conversation_id, "
        + ("c.memory_space" if has_conversation_space else "NULL")
        + " FROM conversations c "
        + ("WHERE " + " AND ".join(where) if where else "")
        + " ORDER BY c.conversation_id",
        params,
    ).fetchall()

    snapshots = []
    for conversation_key, conversation_space in rows:
        message_rows = _messages_for_conversation(conn, message_columns, str(conversation_key))
        resolved_space = str(conversation_space or "")
        if not resolved_space and has_message_space and message_rows:
            resolved_space = str(message_rows[0]["memory_space"] or "")
        if memory_space and resolved_space != memory_space:
            continue
        snapshots.append(_snapshot(str(conversation_key), resolved_space, message_rows))
    return snapshots


def _messages_for_conversation(
    conn: sqlite3.Connection, columns: set[str], conversation_id: str
) -> list[dict[str, Any]]:
    def expression(name: str, fallback: str = "NULL") -> str:
        return name if name in columns else fallback

    query = f"""
        SELECT {expression('message_id')} AS message_id,
               {expression('role')} AS role,
               {expression('text', expression('content'))} AS content,
               {expression('create_time_iso', expression('created_at'))} AS created_at,
               {expression('memory_space')} AS memory_space
        FROM messages WHERE conversation_id = ?
    """
    cursor = conn.execute(query, (conversation_id,))
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _snapshot(
    conversation_id: str, memory_space: str, rows: list[dict[str, Any]]
) -> dict[str, Any]:
    canonical = sorted(
        (
            str(row.get("message_id") or ""),
            str(row.get("role") or ""),
            str(row.get("content") or ""),
        )
        for row in rows
    )
    serialized = json.dumps(canonical, ensure_ascii=False, separators=(",", ":"))
    timestamps = sorted(str(row.get("created_at") or "") for row in rows if row.get("created_at"))
    latest_id = max((entry[0] for entry in canonical), default=None) or None
    return {
        "conversation_id": conversation_id,
        "memory_space": memory_space,
        "message_count": len(canonical),
        "first_message_at": timestamps[0] if timestamps else None,
        "last_message_at": timestamps[-1] if timestamps else None,
        "latest_message_id": latest_id,
        "source_content_hash": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
        "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
    }
