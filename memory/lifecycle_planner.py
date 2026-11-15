from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from memory.embedding_provider import get_embedding_provider


UP_TO_DATE = "up_to_date"
EMBEDDING_MISSING = "embedding_missing"
EMBEDDING_STALE = "embedding_stale"
EMBEDDING_IDENTITY_MISMATCH = "embedding_identity_mismatch"
LEGACY_UNVERSIONED = "legacy_unversioned"
SUMMARY_EMPTY = "summary_empty"
BLOCKED = "blocked"
SEMANTIC_TABLE_MISSING = "semantic_table_missing"

ACTION_NONE = "none"
ACTION_CREATE_EMBEDDING = "create_embedding"
ACTION_REBUILD_EMBEDDING = "rebuild_embedding"
ACTION_MIGRATE_LEGACY_EMBEDDING = "migrate_legacy_embedding"
ACTION_REVIEW_SUMMARY = "review_summary"
ACTION_BLOCKED = "blocked"
PLAN_SCHEMA_VERSION = "v5.0.1"
CONTROLLED_PLAN_SCHEMA_VERSION = "v5.4"

_EMBEDDING_COLUMNS = {
    "source_table",
    "source_id",
    "embedding_provider",
    "embedding_model",
    "model_revision",
    "model_fingerprint",
    "embedding_protocol",
    "embedding_normalized",
    "embedding_dim",
    "content_hash",
}


def plan_memory_lifecycle(
    source_db: str | Path,
    semantic_db: str | Path,
    memory_space: str | None = None,
    provider_name: str = "local_sentence_transformer",
    memory_sidecar_path: str | Path | None = None,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    """Build a read-only semantic-index maintenance plan without executing it."""
    identity = _provider_identity(provider_name)
    base = {
        "readonly": True,
        "executed": False,
        "provider_identity": _safe_identity(identity),
        "summary_count": 0,
        "up_to_date_count": 0,
        "action_required_count": 0,
        "status_counts": {},
        "action_counts": {},
        "items": [],
        "warnings": [],
    }

    try:
        summaries = (
            _load_unified_summaries(source_db, memory_sidecar_path, memory_space, conversation_id)
            if memory_sidecar_path is not None
            else _load_summaries(source_db, memory_space, conversation_id)
        )
    except sqlite3.Error:
        return _blocked_plan(base, "memory_summaries cannot be read")

    if summaries is None:
        return _blocked_plan(base, "memory_summaries table is missing")

    try:
        embeddings, semantic_missing = _load_embeddings(
            semantic_db,
            memory_space,
            {"memory_summaries", "summary_versions"} if memory_sidecar_path is not None else {"memory_summaries"},
        )
    except sqlite3.Error:
        return _blocked_plan(base, "memory_embeddings cannot be read", summary_count=len(summaries))

    if semantic_missing:
        items = [_semantic_table_missing_item(summary) for summary in summaries]
        return _finalize(base, items, ["memory_embeddings table is missing"], SEMANTIC_TABLE_MISSING)

    by_source_id: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for embedding in embeddings:
        key = (str(embedding["source_table"]), str(embedding["source_id"]))
        by_source_id.setdefault(key, []).append(embedding)

    items = [
        _plan_summary(
            summary,
            by_source_id.get((str(summary.get("source_table") or "memory_summaries"), str(summary["id"])), []),
            identity,
        )
        for summary in summaries
    ]
    return _finalize(base, items, [])


def _provider_identity(provider_name: str) -> dict[str, Any]:
    provider = get_embedding_provider(provider_name)
    identity = provider.get_embedding_identity()
    required = {
        "provider_name",
        "model_name",
        "model_revision",
        "model_fingerprint",
        "embedding_protocol",
        "embedding_dim",
        "normalized",
    }
    if set(identity) != required:
        raise ValueError("Embedding provider identity is incomplete")
    return identity


def get_lifecycle_provider_identity(provider_name: str = "local_sentence_transformer") -> dict[str, Any]:
    """Return the complete, path-free provider identity used by confirmed plans."""
    identity = _provider_identity(provider_name)
    return {
        "provider": identity["provider_name"],
        "model": identity["model_name"],
        "revision": identity["model_revision"],
        "fingerprint": identity["model_fingerprint"],
        "protocol": identity["embedding_protocol"],
        "dim": identity["embedding_dim"],
        "normalized": bool(identity["normalized"]),
    }


def calculate_lifecycle_plan_id(
    target_kind: str,
    provider_identity: dict[str, Any],
    items: list[dict[str, Any]],
    plan_schema_version: str = PLAN_SCHEMA_VERSION,
) -> str:
    """Hash a canonical complete plan envelope without paths or source text."""
    payload = {
        "plan_schema_version": plan_schema_version,
        "target_kind": target_kind,
        "provider_identity": provider_identity,
        "items": sorted(
            items,
            key=lambda item: (
                str(item["summary_id"]),
                str(item["memory_space"]),
                str(item["action"]),
                str(item["content_hash"]),
            ),
        ),
    }
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _safe_identity(identity: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider": identity["provider_name"],
        "model": identity["model_name"],
        "revision": identity["model_revision"],
        "fingerprint": str(identity["model_fingerprint"])[:12],
        "protocol": identity["embedding_protocol"],
        "dim": identity["embedding_dim"],
        "normalized": bool(identity["normalized"]),
    }


def _load_summaries(
    db_path: str | Path,
    memory_space: str | None,
    conversation_id: str | None = None,
) -> list[dict[str, Any]] | None:
    conn = _connect_readonly(db_path)
    try:
        if not _table_exists(conn, "memory_summaries"):
            return None
        clauses = []
        params: list[str] = []
        if memory_space is not None:
            clauses.append("memory_space = ?")
            params.append(memory_space)
        if conversation_id is not None:
            clauses.append("conversation_id = ?")
            params.append(conversation_id)
        where_sql = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT id, memory_space, summary_type, conversation_id, summary_text
            FROM memory_summaries
            {where_sql}
            ORDER BY id ASC
            """,
            params,
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _load_unified_summaries(
    source_db: str | Path,
    memory_sidecar_path: str | Path,
    memory_space: str | None,
    conversation_id: str | None,
) -> list[dict[str, Any]]:
    """Prefer latest accepted sidecar versions, then latest legacy summaries."""
    from memory.summary_repository import latest_accepted_summaries

    accepted = latest_accepted_summaries(memory_sidecar_path, memory_space, conversation_id)
    accepted_by_conversation = {str(row["conversation_id"]): _accepted_summary(row) for row in accepted}
    legacy = _load_latest_legacy_summaries(source_db, memory_space, conversation_id)
    for row in legacy:
        accepted_by_conversation.setdefault(str(row["conversation_id"]), _legacy_summary(row))
    return sorted(
        accepted_by_conversation.values(),
        key=lambda row: (str(row.get("conversation_id") or ""), str(row["id"])),
    )


def _load_latest_legacy_summaries(
    db_path: str | Path,
    memory_space: str | None,
    conversation_id: str | None,
) -> list[dict[str, Any]]:
    from memory.summary_repository import latest_legacy_summaries

    return latest_legacy_summaries(db_path, memory_space, conversation_id)


def _accepted_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["summary_id"]),
        "summary_id": str(row["summary_id"]),
        "summary_source": "memory_sidecar",
        "source_table": "summary_versions",
        "summary_version": int(row["version"]),
        "memory_space": row["memory_space"],
        "summary_type": "conversation_summary",
        "conversation_id": row["conversation_id"],
        "summary_text": row["summary_text"],
        "summary_text_hash": row["summary_text_hash"],
        "candidate_id": row["candidate_id"],
        "acceptance_plan_id": row["acceptance_plan_id"],
        "source_snapshot_hash": row["source_snapshot_hash"],
        "semantic_target_kind": "summary_embedding",
    }


def _legacy_summary(row: dict[str, Any]) -> dict[str, Any]:
    text = str(row.get("summary_text") or "")
    return {
        **row,
        "id": str(row["id"]),
        "summary_id": str(row["id"]),
        "summary_source": "legacy",
        "source_table": "memory_summaries",
        "summary_version": None,
        "summary_text_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "candidate_id": None,
        "acceptance_plan_id": None,
        "source_snapshot_hash": row.get("source_content_hash"),
        "semantic_target_kind": "summary_embedding",
    }


def _load_embeddings(
    db_path: str | Path, memory_space: str | None, source_tables: set[str]
) -> tuple[list[dict[str, Any]], bool]:
    conn = _connect_readonly(db_path)
    try:
        if not _table_exists(conn, "memory_embeddings"):
            return [], True
        columns = {row[1] for row in conn.execute("PRAGMA table_info(memory_embeddings)")}
        if not _EMBEDDING_COLUMNS.issubset(columns):
            raise sqlite3.DatabaseError("memory_embeddings schema is incomplete")
        ordered_tables = sorted(source_tables)
        clauses = [f"source_table IN ({', '.join('?' for _ in ordered_tables)})"]
        params: list[str] = list(ordered_tables)
        if memory_space is not None:
            clauses.append("memory_space = ?")
            params.append(memory_space)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT source_table, source_id, memory_space, embedding_provider, embedding_model,
                   model_revision, model_fingerprint, embedding_protocol,
                   embedding_normalized, embedding_dim, content_hash
            FROM memory_embeddings
            WHERE {' AND '.join(clauses)}
            ORDER BY id ASC
            """,
            params,
        ).fetchall()
        return [dict(row) for row in rows], False
    finally:
        conn.close()


def _plan_summary(summary: dict[str, Any], embeddings: list[dict[str, Any]], identity: dict[str, Any]) -> dict[str, Any]:
    text = str(summary.get("summary_text") or "")
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    base = _item_base(summary, content_hash)
    if not text.strip():
        return {**base, "status": SUMMARY_EMPTY, "recommended_action": ACTION_REVIEW_SUMMARY,
                "reason": "summary text is empty", "matching_embedding_count": len(embeddings),
                "compatible_embedding_count": 0}
    if not embeddings:
        return {**base, "status": EMBEDDING_MISSING, "recommended_action": ACTION_CREATE_EMBEDDING,
                "reason": "no embedding record", "matching_embedding_count": 0,
                "compatible_embedding_count": 0}
    compatible = [row for row in embeddings if _identity_matches(row, identity)]
    if any(row.get("content_hash") == content_hash for row in compatible):
        return {**base, "status": UP_TO_DATE, "recommended_action": ACTION_NONE,
                "reason": "compatible embedding is current", "matching_embedding_count": len(embeddings),
                "compatible_embedding_count": len(compatible)}
    if compatible:
        return {**base, "status": EMBEDDING_STALE, "recommended_action": ACTION_REBUILD_EMBEDDING,
                "reason": "summary content hash changed", "matching_embedding_count": len(embeddings),
                "compatible_embedding_count": len(compatible)}
    non_legacy = [row for row in embeddings if row.get("embedding_provider") != LEGACY_UNVERSIONED]
    if non_legacy:
        return {**base, "status": EMBEDDING_IDENTITY_MISMATCH, "recommended_action": ACTION_REBUILD_EMBEDDING,
                "reason": "embedding identity differs", "matching_embedding_count": len(embeddings),
                "compatible_embedding_count": 0}
    return {**base, "status": LEGACY_UNVERSIONED, "recommended_action": ACTION_MIGRATE_LEGACY_EMBEDDING,
            "reason": "legacy embedding identity", "matching_embedding_count": len(embeddings),
            "compatible_embedding_count": 0}


def _identity_matches(embedding: dict[str, Any], identity: dict[str, Any]) -> bool:
    return (
        embedding.get("embedding_provider") == identity["provider_name"]
        and embedding.get("embedding_model") == identity["model_name"]
        and embedding.get("model_revision") == identity["model_revision"]
        and embedding.get("model_fingerprint") == identity["model_fingerprint"]
        and embedding.get("embedding_protocol") == identity["embedding_protocol"]
        and embedding.get("embedding_dim") == identity["embedding_dim"]
        and int(embedding.get("embedding_normalized") or 0) == int(bool(identity["normalized"]))
    )


def _item_base(summary: dict[str, Any], content_hash: str) -> dict[str, Any]:
    item = {
        "summary_id": str(summary["id"]),
        "memory_space": summary.get("memory_space"),
        "summary_type": summary.get("summary_type"),
        "conversation_id": summary.get("conversation_id"),
        "content_hash": content_hash,
    }
    if summary.get("summary_source"):
        item.update({
            "summary_source": summary["summary_source"],
            "source_table": summary["source_table"],
            "summary_version": summary.get("summary_version"),
            "summary_text_hash": summary["summary_text_hash"],
            "candidate_id": summary.get("candidate_id"),
            "acceptance_plan_id": summary.get("acceptance_plan_id"),
            "source_snapshot_hash": summary.get("source_snapshot_hash"),
            "semantic_target_kind": summary["semantic_target_kind"],
        })
    return item


def _semantic_table_missing_item(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        **_item_base(summary, hashlib.sha256(str(summary.get("summary_text") or "").encode("utf-8")).hexdigest()),
        "status": SEMANTIC_TABLE_MISSING,
        "recommended_action": ACTION_BLOCKED,
        "reason": "memory_embeddings table is missing",
        "matching_embedding_count": 0,
        "compatible_embedding_count": 0,
    }


def _blocked_plan(base: dict[str, Any], reason: str, summary_count: int = 0) -> dict[str, Any]:
    return {
        **base,
        "summary_count": summary_count,
        "status_counts": {BLOCKED: 1},
        "action_counts": {ACTION_BLOCKED: 1},
        "warnings": [reason],
    }


def _finalize(base: dict[str, Any], items: list[dict[str, Any]], warnings: list[str], overall_status: str | None = None) -> dict[str, Any]:
    status_counts: dict[str, int] = {}
    action_counts: dict[str, int] = {}
    for item in items:
        status_counts[item["status"]] = status_counts.get(item["status"], 0) + 1
        action = item["recommended_action"]
        action_counts[action] = action_counts.get(action, 0) + 1
    result = {
        **base,
        "summary_count": len(items),
        "up_to_date_count": status_counts.get(UP_TO_DATE, 0),
        "action_required_count": len(items) - action_counts.get(ACTION_NONE, 0),
        "status_counts": status_counts,
        "action_counts": action_counts,
        "items": items,
        "warnings": warnings,
    }
    if overall_status is not None:
        result["overall_status"] = overall_status
    return result


def _connect_readonly(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path).expanduser().resolve()
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table_name,)
    ).fetchone() is not None
