from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.db_path_policy import validate_test_db_write_target


CREATE_MEMORY_EMBEDDINGS_SQL = """
CREATE TABLE memory_embeddings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_table TEXT NOT NULL,
    source_id TEXT NOT NULL,
    memory_space TEXT NOT NULL DEFAULT 'general',
    source_platform TEXT,
    embedding_provider TEXT NOT NULL,
    embedding_model TEXT NOT NULL,
    model_revision TEXT NOT NULL,
    model_fingerprint TEXT NOT NULL,
    embedding_protocol TEXT NOT NULL,
    embedding_normalized INTEGER NOT NULL,
    embedding_dim INTEGER NOT NULL,
    embedding_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    content_preview TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    metadata_json TEXT,
    UNIQUE(
        source_table, source_id, embedding_provider, embedding_model, model_revision,
        model_fingerprint, embedding_protocol, embedding_normalized, content_hash
    )
)
"""

IDENTITY_COLUMNS = {
    "embedding_provider",
    "embedding_model",
    "model_revision",
    "model_fingerprint",
    "embedding_protocol",
    "embedding_normalized",
    "embedding_dim",
}


def migrate_semantic_memory(db_path: str | Path, write: bool = False) -> dict:
    try:
        resolved = validate_test_db_write_target(db_path)
    except ValueError as exc:
        if write:
            raise
        return {"status": "refused", "write": False, "db_path": str(Path(db_path)), "reason": str(exc)}

    if not write:
        return {"status": "dry_run", "write": False, "db_path": str(resolved)}

    conn = sqlite3.connect(resolved)
    try:
        exists = (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                ("memory_embeddings",),
            ).fetchone()
            is not None
        )
        if not exists:
            conn.execute(CREATE_MEMORY_EMBEDDINGS_SQL)
            conn.commit()
            return {"status": "created", "write": True, "db_path": str(resolved)}
        columns = {row[1] for row in conn.execute("PRAGMA table_info(memory_embeddings)")}
        if IDENTITY_COLUMNS.issubset(columns):
            return {"status": "skipped", "write": True, "db_path": str(resolved)}
        _migrate_legacy_table(conn)
        conn.commit()
        return {"status": "migrated", "write": True, "db_path": str(resolved)}
    finally:
        conn.close()


def _migrate_legacy_table(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE memory_embeddings RENAME TO memory_embeddings_legacy")
    conn.execute(CREATE_MEMORY_EMBEDDINGS_SQL)
    conn.execute(
        """
        INSERT INTO memory_embeddings (
            id, source_table, source_id, memory_space, source_platform,
            embedding_provider, embedding_model, model_revision, model_fingerprint,
            embedding_protocol, embedding_normalized, embedding_dim, embedding_json,
            content_hash, content_preview, created_at, metadata_json
        )
        SELECT
            id, source_table, source_id, memory_space, source_platform,
            'legacy_unversioned', embedding_model, 'legacy_unversioned', 'legacy_unversioned',
            'legacy_unversioned', 0, embedding_dim, embedding_json,
            content_hash, content_preview, created_at, metadata_json
        FROM memory_embeddings_legacy
        """
    )
    conn.execute("DROP TABLE memory_embeddings_legacy")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create the semantic memory test table.")
    parser.add_argument("--db", required=True)
    parser.add_argument("--write", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = migrate_semantic_memory(args.db, write=args.write)
    except ValueError as exc:
        print(
            json.dumps(
                {"status": "refused", "write": args.write, "db_path": args.db, "reason": str(exc)},
                ensure_ascii=True,
                indent=2,
            )
        )
        return 2

    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
