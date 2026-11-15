"""Read and append accepted summary versions without changing legacy records."""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any

from memory.memory_sidecar import sidecar_initialized
from memory.source_snapshot import snapshots_for_database, table_columns, table_exists


def _readonly(path: str | Path) -> sqlite3.Connection:
    target = Path(path).expanduser().resolve()
    return sqlite3.connect(f"file:{target.as_posix()}?mode=ro", uri=True)


def list_summary_versions(sidecar_path: str | Path, conversation_id: str) -> list[dict[str, Any]]:
    if not sidecar_initialized(sidecar_path):
        return []
    conn = _readonly(sidecar_path)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM summary_versions WHERE conversation_id = ? ORDER BY version ASC", (conversation_id,)).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def latest_accepted_summary(sidecar_path: str | Path, conversation_id: str, memory_space: str | None = None) -> dict[str, Any] | None:
    if not sidecar_initialized(sidecar_path):
        return None
    conn = _readonly(sidecar_path)
    try:
        conn.row_factory = sqlite3.Row
        where, params = ["conversation_id = ?"], [conversation_id]
        if memory_space is not None:
            where.append("memory_space = ?"); params.append(memory_space)
        row = conn.execute(f"SELECT * FROM summary_versions WHERE {' AND '.join(where)} ORDER BY version DESC LIMIT 1", params).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def candidate_is_accepted(sidecar_path: str | Path, candidate_id: str) -> bool:
    if not sidecar_initialized(sidecar_path):
        return False
    conn = _readonly(sidecar_path)
    try:
        return bool(conn.execute("SELECT 1 FROM summary_versions WHERE candidate_id = ?", (candidate_id,)).fetchone())
    finally:
        conn.close()


def accepted_summary_by_candidate(sidecar_path: str | Path, candidate_id: str) -> dict[str, Any] | None:
    """Return the exact accepted version for an immutable candidate identity."""
    if not sidecar_initialized(sidecar_path):
        return None
    conn = _readonly(sidecar_path)
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM summary_versions WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def latest_accepted_summaries(
    sidecar_path: str | Path, memory_space: str | None = None, conversation_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return the latest append-only sidecar metadata per conversation, without text."""
    if not sidecar_initialized(sidecar_path):
        return []
    conn = _readonly(sidecar_path)
    try:
        conn.row_factory = sqlite3.Row
        where, params = [], []
        if memory_space is not None:
            where.append("memory_space = ?"); params.append(memory_space)
        if conversation_id is not None:
            where.append("conversation_id = ?"); params.append(conversation_id)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        rows = conn.execute(f"SELECT * FROM summary_versions{clause} ORDER BY conversation_id ASC, version DESC", params).fetchall()
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = dict(row); latest.setdefault(str(item["conversation_id"]), item)
        return list(latest.values())
    finally:
        conn.close()


def accepted_summary_versions(
    sidecar_path: str | Path,
    memory_space: str | None = None,
    conversation_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return every accepted version needed for version-aware graph construction."""
    if not sidecar_initialized(sidecar_path):
        return []
    conn = _readonly(sidecar_path)
    try:
        conn.row_factory = sqlite3.Row
        where, params = [], []
        if memory_space is not None:
            where.append("memory_space = ?")
            params.append(memory_space)
        if conversation_id is not None:
            where.append("conversation_id = ?")
            params.append(conversation_id)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        rows = conn.execute(
            f"SELECT * FROM summary_versions{clause} ORDER BY conversation_id ASC, version ASC",
            params,
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def latest_legacy_summaries(
    source_db: str | Path,
    memory_space: str | None = None,
    conversation_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return the latest legacy conversation summary per conversation, read-only."""
    conn = _readonly(source_db)
    try:
        if not table_exists(conn, "memory_summaries"):
            return []
        columns = table_columns(conn, "memory_summaries")
        if not {"id", "conversation_id", "summary_text"}.issubset(columns):
            raise sqlite3.DatabaseError("memory_summaries schema is incomplete")
        where: list[str] = []
        params: list[str] = []
        if memory_space is not None and "memory_space" in columns:
            where.append("memory_space = ?")
            params.append(memory_space)
        if conversation_id is not None:
            where.append("conversation_id = ?")
            params.append(conversation_id)
        if "summary_type" in columns:
            where.append("summary_type = 'conversation_summary'")
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        order = "created_at DESC, rowid DESC" if "created_at" in columns else "rowid DESC"
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT * FROM memory_summaries{clause} ORDER BY conversation_id ASC, {order}",
            params,
        ).fetchall()
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            if item.get("conversation_id") is not None:
                item.setdefault("memory_space", "general")
                item.setdefault("summary_type", "conversation_summary")
                item.setdefault("source_content_hash", None)
                latest.setdefault(str(item["conversation_id"]), item)
        return list(latest.values())
    finally:
        conn.close()


def graph_summary_sources(
    sidecar_path: str | Path,
    source_db: str | Path,
    *,
    memory_space: str | None = None,
    conversation_id: str | None = None,
    include_legacy: bool = False,
) -> list[dict[str, Any]]:
    """Return canonical summary inputs while preserving sidecar-over-legacy precedence."""
    accepted = accepted_summary_versions(sidecar_path, memory_space, conversation_id)
    accepted_conversations = {str(row["conversation_id"]) for row in accepted}
    legacy = latest_legacy_summaries(source_db, memory_space, conversation_id)
    selected_legacy = [
        row for row in legacy
        if include_legacy or str(row["conversation_id"]) not in accepted_conversations
    ]

    sources: list[dict[str, Any]] = []
    for row in accepted:
        sources.append({
            "source_table": "summary_versions",
            "source_id": str(row["summary_id"]),
            "summary_source": "memory_sidecar",
            "conversation_id": str(row["conversation_id"]),
            "memory_space": str(row["memory_space"]),
            "summary_version": int(row["version"]),
            "summary_text": str(row["summary_text"]),
            "summary_text_hash": str(row["summary_text_hash"]),
            "source_snapshot_hash": str(row["source_snapshot_hash"]),
        })

    if selected_legacy:
        snapshots = {
            str(item["conversation_id"]): item
            for item in snapshots_for_database(source_db, memory_space, conversation_id)
        }
        for row in selected_legacy:
            conversation_key = str(row["conversation_id"])
            text = str(row.get("summary_text") or "")
            snapshot = snapshots.get(conversation_key, {})
            sources.append({
                "source_table": "memory_summaries",
                "source_id": str(row["id"]),
                "summary_source": "legacy",
                "conversation_id": conversation_key,
                "memory_space": str(row.get("memory_space") or snapshot.get("memory_space") or "general"),
                "summary_version": None,
                "summary_text": text,
                "summary_text_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "source_snapshot_hash": snapshot.get("source_content_hash"),
            })
    return sorted(
        sources,
        key=lambda row: (
            str(row["conversation_id"]),
            str(row["source_table"]),
            -1 if row["summary_version"] is None else int(row["summary_version"]),
            str(row["source_id"]),
        ),
    )


def next_version(sidecar_path: str | Path, conversation_id: str) -> int:
    latest = latest_accepted_summary(sidecar_path, conversation_id)
    return int(latest["version"]) + 1 if latest else 1


def legacy_summary(source_db: str | Path, conversation_id: str, memory_space: str) -> dict[str, Any] | None:
    rows = latest_legacy_summaries(source_db, memory_space, conversation_id)
    return rows[0] if rows else None


def current_summary(sidecar_path: str | Path, source_db: str | Path, conversation_id: str, memory_space: str) -> dict[str, Any] | None:
    accepted = latest_accepted_summary(sidecar_path, conversation_id, memory_space)
    if accepted:
        return {"summary_source": "memory_sidecar", "summary_version": accepted["version"], **accepted}
    legacy = legacy_summary(source_db, conversation_id, memory_space)
    return {"summary_source": "legacy", "summary_version": None, **legacy} if legacy else None


def _append_summary_record(conn: sqlite3.Connection, record: dict[str, Any]) -> None:
    """Internal transaction primitive. The public production gateway is v5.3 acceptance only."""
    if hashlib.sha256(str(record["summary_text"]).encode("utf-8")).hexdigest() != record["summary_text_hash"]:
        raise ValueError("summary_text_hash does not match summary_text")
    columns = (
        "summary_id", "conversation_id", "memory_space", "version", "candidate_id", "candidate_schema_version",
        "source_snapshot_hash", "source_message_count", "source_last_message_at", "source_latest_message_id",
        "generator_identity", "generator_config_hash", "summary_text", "summary_text_hash", "reason",
        "base_summary_id", "base_summary_text_hash", "acceptance_plan_id", "acceptance_run_id", "accepted_at", "created_at",
    )
    conn.execute(f"INSERT INTO summary_versions ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})", [record.get(name) for name in columns])
