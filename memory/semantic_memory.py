from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from memory import db_path_policy
from memory.db_path_policy import SemanticSidecarPathError, validate_test_db_write_target
from memory.embedding_provider import cosine_similarity, get_embedding_provider


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REAL_DB_PATH = PROJECT_ROOT / "data" / ("ayunos" + "_chatgpt" + ".db")
MEMORY_EMBEDDINGS_TABLE = "memory_embeddings"
REQUIRED_EMBEDDING_COLUMNS = {
    "source_table", "source_id", "memory_space", "source_platform",
    "embedding_provider", "embedding_model", "model_revision",
    "model_fingerprint", "embedding_protocol", "embedding_normalized",
    "embedding_dim", "embedding_json", "content_hash", "content_preview",
    "created_at", "metadata_json",
}


class SemanticRetrievalError(RuntimeError):
    pass


def build_embedding_record(
    source_table: str,
    source_id: str,
    text: str,
    memory_space: str = "general",
    source_platform: str | None = None,
    provider_name: str = "deterministic_test",
    metadata: dict | None = None,
) -> dict:
    provider = get_embedding_provider(provider_name)
    return _build_embedding_record_with_provider(
        source_table, source_id, text, memory_space, source_platform, provider, metadata
    )


def _build_embedding_record_with_provider(
    source_table: str,
    source_id: str,
    text: str,
    memory_space: str,
    source_platform: str | None,
    provider: Any,
    metadata: dict | None,
) -> dict:
    return _build_embedding_record_with_embedding(
        source_table,
        source_id,
        text,
        memory_space,
        source_platform,
        provider,
        provider.embed_text(text),
        metadata,
    )


def _build_embedding_record_with_embedding(
    source_table: str,
    source_id: str,
    text: str,
    memory_space: str,
    source_platform: str | None,
    provider: Any,
    embedding: list[float],
    metadata: dict | None,
) -> dict:
    preview = " ".join(text.split())[:240]
    identity = provider.get_embedding_identity()
    _validate_provider_identity(identity, provider)

    return {
        "source_table": source_table,
        "source_id": source_id,
        "memory_space": memory_space,
        "source_platform": source_platform,
        "embedding_provider": identity["provider_name"],
        "embedding_model": identity["model_name"],
        "model_revision": identity["model_revision"],
        "model_fingerprint": identity["model_fingerprint"],
        "embedding_protocol": identity["embedding_protocol"],
        "embedding_normalized": int(bool(identity["normalized"])),
        "embedding_dim": len(embedding),
        "embedding_json": json.dumps(embedding, ensure_ascii=True, separators=(",", ":")),
        "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "content_preview": preview,
        "metadata_json": json.dumps(metadata or {}, ensure_ascii=True, sort_keys=True),
    }


def build_embedding_records_from_summaries(
    rows: list[dict],
    provider_name: str = "deterministic_test",
) -> tuple[list[dict], int]:
    valid_rows = []
    skipped = 0
    for row in rows:
        summary_text = str(row.get("summary_text") or "")
        if not summary_text.strip():
            skipped += 1
            continue
        valid_rows.append((row, summary_text))

    if not valid_rows:
        return [], skipped

    provider = get_embedding_provider(provider_name)
    vectors = provider.embed_texts([summary_text for _, summary_text in valid_rows])
    if len(vectors) != len(valid_rows):
        raise RuntimeError("Embedding provider returned an unexpected batch size")

    records = []
    for (row, summary_text), embedding in zip(valid_rows, vectors):
        if len(embedding) != provider.dim:
            raise RuntimeError("Embedding provider returned an unexpected vector dimension")
        metadata = {
            "summary_source": "legacy",
            "summary_id": str(row["id"]),
            "summary_type": row.get("summary_type"),
            "conversation_id": row.get("conversation_id"),
            "summary_version": None,
            "summary_text_hash": hashlib.sha256(summary_text.encode("utf-8")).hexdigest(),
            "source_snapshot_hash": row.get("source_snapshot_hash") or row.get("source_content_hash"),
            "memory_space": row.get("memory_space") or "general",
            "semantic_target_kind": "summary_embedding",
            "method": row.get("method"),
            "source_message_count": row.get("source_message_count"),
        }
        record = _build_embedding_record_with_embedding(
            "memory_summaries",
            str(row["id"]),
            summary_text,
            row.get("memory_space") or "general",
            None,
            provider,
            embedding,
            metadata,
        )
        record["content_preview"] = summary_text[:200]
        records.append(record)
    return records, skipped


def rank_by_semantic_similarity(
    query: str,
    records: list[dict],
    provider_name: str = "deterministic_test",
    top_k: int = 5,
) -> list[dict]:
    provider = get_embedding_provider(provider_name)
    identity = provider.get_embedding_identity()
    _validate_provider_identity(identity, provider)
    query_embedding = provider.embed_text(query)
    ranked = []

    for record in records:
        _validate_record_identity(record, identity)
        embedding = _load_embedding(record)
        try:
            stored_dim = int(record.get("embedding_dim", len(embedding)))
        except (TypeError, ValueError) as exc:
            raise SemanticRetrievalError("Stored embedding dimension is invalid") from exc
        if stored_dim != len(embedding) or len(embedding) != len(query_embedding):
            raise SemanticRetrievalError("Stored embedding dimension is incompatible")
        scored = dict(record)
        scored["score"] = cosine_similarity(query_embedding, embedding)
        ranked.append(scored)

    ranked.sort(key=lambda item: item["score"], reverse=True)
    return ranked[: max(0, top_k)]


def save_embedding_record(
    record: dict,
    db_path: str | Path,
    dry_run: bool = True,
    allow_production_sidecar: bool = False,
) -> dict:
    path = Path(db_path).expanduser()
    if dry_run:
        return {"dry_run": True, "written": False, "db_path": str(path)}

    if path.resolve(strict=False) == db_path_policy.DEFAULT_SEMANTIC_SIDECAR_PATH.resolve(strict=False):
        raise SemanticSidecarPathError(
            "Production semantic writes require the confirmed lifecycle maintenance gateway"
        )
    resolved = validate_test_db_write_target(path)
    if not resolved.exists():
        raise RuntimeError("Database file does not exist; refusing to create it")

    conn = sqlite3.connect(resolved)
    try:
        if not _table_exists(conn, MEMORY_EMBEDDINGS_TABLE):
            raise RuntimeError("memory_embeddings table is missing; refusing to auto-create it")
        try:
            insert_embedding_record(conn, record)
        except sqlite3.IntegrityError as exc:
            if is_unique_constraint_error(exc):
                return {
                    "dry_run": False,
                    "written": False,
                    "status": "duplicate",
                    "db_path": str(resolved),
                }
            raise
        conn.commit()
    finally:
        conn.close()

    return {"dry_run": False, "written": True, "status": "inserted", "db_path": str(resolved)}


def insert_embedding_record(conn: sqlite3.Connection, record: dict) -> None:
    """Insert one current-schema record using the shared write primitive."""
    conn.execute(
        """
        INSERT INTO memory_embeddings (
            source_table, source_id, memory_space, source_platform,
            embedding_provider, embedding_model, model_revision, model_fingerprint,
            embedding_protocol, embedding_normalized, embedding_dim, embedding_json,
            content_hash, content_preview, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        tuple(
            record.get(key)
            for key in (
                "source_table", "source_id", "memory_space", "source_platform",
                "embedding_provider", "embedding_model", "model_revision", "model_fingerprint",
                "embedding_protocol", "embedding_normalized", "embedding_dim", "embedding_json",
                "content_hash", "content_preview", "metadata_json",
            )
        ),
    )


def is_unique_constraint_error(exc: sqlite3.IntegrityError) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    name = getattr(exc, "sqlite_errorname", None)
    allowed_codes = {
        getattr(sqlite3, "SQLITE_CONSTRAINT_UNIQUE", 2067),
        getattr(sqlite3, "SQLITE_CONSTRAINT_PRIMARYKEY", 1555),
    }
    return code in allowed_codes or name in {"SQLITE_CONSTRAINT_UNIQUE", "SQLITE_CONSTRAINT_PRIMARYKEY"}


def is_busy_or_locked_error(exc: sqlite3.OperationalError) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    name = getattr(exc, "sqlite_errorname", None)
    allowed_codes = {getattr(sqlite3, "SQLITE_BUSY", 5), getattr(sqlite3, "SQLITE_LOCKED", 6)}
    return code in allowed_codes or name in {"SQLITE_BUSY", "SQLITE_LOCKED"}


def load_embedding_records(
    db_path: str | Path,
    memory_space: str | None = None,
    source_table: str | None = None,
    embedding_model: str | None = None,
    limit: int = 100,
) -> list[dict]:
    path = Path(db_path).expanduser().resolve()
    if not path.exists():
        return []

    bounded_limit = max(0, min(int(limit), 1000))
    clauses = []
    params: list[str] = []
    if memory_space is not None:
        clauses.append("memory_space = ?")
        params.append(memory_space)
    if source_table is not None:
        clauses.append("source_table = ?")
        params.append(source_table)
    if embedding_model is not None:
        clauses.append("embedding_model = ?")
        params.append(embedding_model)

    where_sql = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"""
        SELECT
            id,
            source_table,
            source_id,
            memory_space,
            source_platform,
            embedding_provider,
            embedding_model,
            model_revision,
            model_fingerprint,
            embedding_protocol,
            embedding_normalized,
            embedding_dim,
            embedding_json,
            content_hash,
            content_preview,
            created_at,
            metadata_json
        FROM memory_embeddings
        {where_sql}
        ORDER BY id ASC
        LIMIT ?
    """

    try:
        conn = connect_readonly(path)
        try:
            if not _table_exists(conn, MEMORY_EMBEDDINGS_TABLE):
                return []
            _ensure_embedding_schema(conn)
            rows = conn.execute(sql, (*params, bounded_limit)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise SemanticRetrievalError("Semantic index read failed") from exc

    return [_row_to_record(row) for row in rows]


def semantic_search_records(
    query: str,
    db_path: str | Path,
    memory_space: str | None = None,
    top_k: int = 5,
    provider_name: str = "deterministic_test",
) -> list[dict]:
    provider = get_embedding_provider(provider_name)
    identity = provider.get_embedding_identity()
    records = _select_compatible_records(load_embedding_records(
        db_path,
        memory_space=memory_space,
        limit=max(top_k, 100),
    ), identity)
    ranked = rank_by_semantic_similarity(query, records, provider_name=provider_name, top_k=top_k)
    return [
        {
            "score": item["score"],
            "source_table": item["source_table"],
            "source_id": item["source_id"],
            "memory_space": item["memory_space"],
            "content_preview": item.get("content_preview"),
        }
        for item in ranked
    ]


def semantic_summary_search_preview(
    query: str,
    db_path: str | Path,
    memory_space: str | None = None,
    top_k: int = 5,
    provider_name: str = "deterministic_test",
    min_score: float | None = None,
) -> dict:
    bounded_top_k = max(1, min(int(top_k), 20))
    if min_score is not None:
        min_score = float(min_score)
        if min_score < -1.0 or min_score > 1.0:
            raise ValueError("min_score must be between -1 and 1")

    provider = get_embedding_provider(provider_name)
    identity = provider.get_embedding_identity()
    _validate_provider_identity(identity, provider)
    base = {
        "query": query,
        "provider": identity["provider_name"],
        "embedding_model": identity["model_name"],
        "embedding_dim": provider.dim,
        "model_revision": identity["model_revision"],
        "model_fingerprint": identity["model_fingerprint"][:12],
        "embedding_protocol": identity["embedding_protocol"],
        "embedding_normalized": bool(identity["normalized"]),
        "candidate_count": 0,
        "result_count": 0,
        "memory_space": memory_space,
        "top_k": bounded_top_k,
        "min_score": min_score,
        "results": [],
    }

    path = Path(db_path).expanduser().resolve()
    if not path.exists():
        return {**base, "readonly_error": "semantic index database is missing"}

    try:
        conn = connect_readonly(path)
        try:
            if not _table_exists(conn, MEMORY_EMBEDDINGS_TABLE):
                return {**base, "readonly_error": "memory_embeddings table is missing"}
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise SemanticRetrievalError("Semantic index read failed") from exc

    candidates = _select_compatible_records(load_embedding_records(
        path,
        memory_space=memory_space,
        limit=1000,
    ), identity)
    candidates = [
        record for record in candidates
        if record.get("source_table") in {"memory_summaries", "summary_versions"}
    ]
    ranked = rank_by_semantic_similarity(query, candidates, provider_name=provider_name, top_k=len(candidates) or 1)
    if min_score is not None:
        ranked = [record for record in ranked if float(record["score"]) >= min_score]
    limited = ranked[:bounded_top_k]

    results = []
    for rank, record in enumerate(limited, start=1):
        metadata = _safe_metadata(record.get("metadata_json"))
        results.append(
            {
                "rank": rank,
                "score": record["score"],
                "source_id": record["source_id"],
                "source_table": record["source_table"],
                "memory_space": record["memory_space"],
                "content_preview": record.get("content_preview"),
                "summary_type": metadata.get("summary_type"),
                "summary_source": metadata.get("summary_source", "legacy"),
                "summary_version": metadata.get("summary_version"),
                "summary_text_hash": metadata.get("summary_text_hash"),
                "conversation_id": metadata.get("conversation_id"),
                "method": metadata.get("method"),
                "source_message_count": metadata.get("source_message_count"),
                "model_fingerprint": record["model_fingerprint"][:12],
            }
        )

    return {
        **base,
        "candidate_count": len(candidates),
        "result_count": len(results),
        "results": results,
    }


def ensure_test_database_path(db_path: str | Path) -> Path:
    return validate_test_db_write_target(db_path)


def connect_readonly(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path).expanduser().resolve()
    uri = f"file:{path.as_posix()}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _load_embedding(record: dict) -> list[float]:
    try:
        value: Any = json.loads(record["embedding_json"])
        if not isinstance(value, list):
            raise TypeError("embedding is not a list")
        return [float(item) for item in value]
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise SemanticRetrievalError("Stored embedding vector is invalid") from exc


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        is not None
    )


def _ensure_embedding_schema(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(memory_embeddings)")}
    missing = REQUIRED_EMBEDDING_COLUMNS - columns
    if missing:
        raise SemanticRetrievalError("Semantic index schema is incomplete")


def _row_to_record(row: sqlite3.Row | tuple) -> dict:
    return {
        "id": row[0],
        "source_table": row[1],
        "source_id": row[2],
        "memory_space": row[3],
        "source_platform": row[4],
        "embedding_provider": row[5],
        "embedding_model": row[6],
        "model_revision": row[7],
        "model_fingerprint": row[8],
        "embedding_protocol": row[9],
        "embedding_normalized": row[10],
        "embedding_dim": row[11],
        "embedding_json": row[12],
        "content_hash": row[13],
        "content_preview": row[14],
        "created_at": row[15],
        "metadata_json": row[16],
    }


def _safe_metadata(value: str | None) -> dict:
    if not value:
        return {}
    try:
        metadata = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return metadata if isinstance(metadata, dict) else {}


def _validate_provider_identity(identity: dict, provider: Any) -> None:
    required = {
        "provider_name",
        "model_name",
        "model_revision",
        "model_fingerprint",
        "embedding_protocol",
        "embedding_dim",
        "normalized",
    }
    if set(identity) != required or identity["embedding_dim"] != provider.dim:
        raise SemanticRetrievalError("Embedding provider identity is incomplete")


def _validate_record_identity(record: dict, identity: dict) -> None:
    if record.get("embedding_provider") == "legacy_unversioned":
        raise SemanticRetrievalError("Stored embedding identity is legacy_unversioned")
    expected = {
        "embedding_provider": identity["provider_name"],
        "embedding_model": identity["model_name"],
        "model_revision": identity["model_revision"],
        "model_fingerprint": identity["model_fingerprint"],
        "embedding_protocol": identity["embedding_protocol"],
        "embedding_normalized": int(bool(identity["normalized"])),
        "embedding_dim": identity["embedding_dim"],
    }
    if any(record.get(key) != value for key, value in expected.items()):
        raise SemanticRetrievalError("Stored embedding identity is incompatible")


def _select_compatible_records(records: list[dict], identity: dict) -> list[dict]:
    model_records = [record for record in records if record.get("embedding_model") == identity["model_name"]]
    current = [record for record in model_records if record.get("embedding_provider") == identity["provider_name"]]
    for record in current:
        _validate_record_identity(record, identity)
    if current:
        return current
    if any(record.get("embedding_provider") == "legacy_unversioned" for record in model_records):
        raise SemanticRetrievalError("Stored embedding identity is legacy_unversioned")
    return []
