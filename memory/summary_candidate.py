"""Read-only deterministic summary candidates for controlled review."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from memory.change_detector import detect_changes
from memory.source_snapshot import connect_readonly, snapshots_from_connection, table_columns, table_exists
from memory.summarizer import METHOD, build_conversation_summary


CANDIDATE_SCHEMA_VERSION = 1
GENERATOR_IDENTITY = "yunos.extractive_v1.summary_candidate"
GENERATOR_CONFIG = {"max_messages": 200, "max_text_chars": 1200, "method": METHOD}
ALLOWED_REASONS = {"summary_missing", "coverage_unknown", "new_messages", "source_changed"}
ALLOWED_STATES = {"candidate_ready", "unsupported_content", "empty_source", "generation_failed", "manual_review"}
WINDOWS_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|(?:^|\s)/(?:[^\s/]+/)+)")


def generator_config_hash(config: dict[str, Any] | None = None) -> str:
    payload = json.dumps(config or GENERATOR_CONFIG, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def preview_summary_candidates(
    db_path: str | Path,
    limit: int = 5,
    conversation_id: str | None = None,
    memory_space: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """Generate candidates in memory only; neither database is ever opened writable."""
    clean_limit = max(1, min(int(limit), 50))
    if memory_space not in (None, "", "general", "whale"):
        raise ValueError("memory_space must be general or whale")
    if status not in (None, "", *ALLOWED_REASONS):
        raise ValueError("status must be a supported candidate reason")
    detected = detect_changes(db_path, memory_space or None, conversation_id or None)
    items = [item for item in detected["items"] if item["status"] in ALLOWED_REASONS]
    if status:
        items = [item for item in items if item["status"] == status]
    items = items[:clean_limit]
    results = [generate_summary_candidate(db_path, item["conversation_id"], item["memory_space"], item["status"]) for item in items]
    return {"schema_version": CANDIDATE_SCHEMA_VERSION, "readonly": True, "limit": clean_limit, "candidate_count": len(results), "items": results}


def generate_summary_candidate(
    db_path: str | Path, conversation_id: str, memory_space: str, reason: str,
    generator_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if reason not in ALLOWED_REASONS:
        raise ValueError("reason must be a supported candidate reason")
    config = dict(generator_config or GENERATOR_CONFIG)
    snapshot, rows, old_text = _load_source(db_path, conversation_id, memory_space)
    base = {"conversation_id": conversation_id, "memory_space": memory_space, "reason": reason}
    if snapshot is None or not rows:
        return {**base, "status": "empty_source"}
    non_system = [row for row in rows if str(row.get("role") or "").lower() != "system"]
    if not non_system:
        return {**base, "status": "unsupported_content"}
    if any(not isinstance(row.get("content"), str) for row in non_system):
        return {**base, "status": "unsupported_content"}
    try:
        summary = build_conversation_summary(conversation_id, memory_space, int(config["max_messages"]), db_path)
        candidate_text = str(summary["summary_text"])
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        return {**base, "status": "generation_failed"}
    if not candidate_text or WINDOWS_PATH.search(candidate_text):
        return {**base, "status": "manual_review"}
    config_hash = generator_config_hash(config)
    candidate_id = _candidate_id(conversation_id, snapshot["source_content_hash"], GENERATOR_IDENTITY, config_hash)
    candidate = {
        "candidate_schema_version": CANDIDATE_SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "conversation_id": conversation_id,
        "memory_space": memory_space,
        "source_snapshot_hash": snapshot["source_content_hash"],
        "source_message_count": snapshot["message_count"],
        "source_last_message_at": snapshot["last_message_at"],
        "source_latest_message_id": snapshot["latest_message_id"],
        "generator_identity": GENERATOR_IDENTITY,
        "generator_config_hash": config_hash,
        "candidate_text": candidate_text,
        "candidate_text_hash": _hash(candidate_text),
        "reason": reason,
        # Source-bound created_at is deterministic and records a reviewable source point.
        "created_at": snapshot["last_message_at"] or "",
    }
    result = {"status": "candidate_ready", "candidate": candidate}
    if reason != "summary_missing" and old_text is not None:
        result["difference"] = {"old_text_hash": _hash(old_text), "candidate_text_hash": candidate["candidate_text_hash"], "changed": old_text != candidate_text, "length_delta": len(candidate_text) - len(old_text)}
        result["_old_text"] = old_text
    return result


def public_preview(payload: dict[str, Any], show_text: bool = False) -> dict[str, Any]:
    """Remove candidate and source text unless the operator explicitly asks for it."""
    result = json.loads(json.dumps(payload, ensure_ascii=False))
    for item in result["items"]:
        candidate = item.get("candidate")
        if candidate and not show_text:
            candidate.pop("candidate_text", None)
        old_text = item.pop("_old_text", None)
        if show_text and old_text is not None:
            item["old_summary_text"] = old_text
    return result


def _load_source(db_path: str | Path, conversation_id: str, memory_space: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]], str | None]:
    conn = connect_readonly(db_path)
    try:
        snapshots = snapshots_from_connection(conn, memory_space, conversation_id)
        snapshot = snapshots[0] if snapshots else None
        rows = _message_rows(conn, conversation_id) if snapshot else []
        old_text = _old_summary_text(conn, conversation_id, memory_space)
        return snapshot, rows, old_text
    finally:
        conn.close()


def _message_rows(conn: sqlite3.Connection, conversation_id: str) -> list[dict[str, Any]]:
    columns = table_columns(conn, "messages")
    content = "text" if "text" in columns else ("content" if "content" in columns else "NULL")
    role = "role" if "role" in columns else "NULL"
    cursor = conn.execute(f"SELECT {role} AS role, {content} AS content FROM messages WHERE conversation_id = ? ORDER BY rowid", (conversation_id,))
    return [dict(zip(("role", "content"), row)) for row in cursor.fetchall()]


def _old_summary_text(conn: sqlite3.Connection, conversation_id: str, memory_space: str) -> str | None:
    if not table_exists(conn, "memory_summaries"):
        return None
    columns = table_columns(conn, "memory_summaries")
    if not {"conversation_id", "summary_text"} <= columns:
        return None
    where, params = ["conversation_id = ?"], [conversation_id]
    if "memory_space" in columns:
        where.append("memory_space = ?"); params.append(memory_space)
    if "summary_type" in columns:
        where.append("summary_type = 'conversation_summary'")
    order = "created_at DESC, rowid DESC" if "created_at" in columns else "rowid DESC"
    row = conn.execute(f"SELECT summary_text FROM memory_summaries WHERE {' AND '.join(where)} ORDER BY {order} LIMIT 1", params).fetchone()
    return str(row[0]) if row and row[0] is not None else None


def _candidate_id(conversation_id: str, snapshot_hash: str, identity: str, config_hash: str) -> str:
    return _hash(json.dumps([conversation_id, snapshot_hash, identity, config_hash], ensure_ascii=False, separators=(",", ":")))


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
