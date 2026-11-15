from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.semantic_memory import (
    MEMORY_EMBEDDINGS_TABLE,
    REAL_DB_PATH,
    build_embedding_records_from_summaries,
    connect_readonly,
    ensure_test_database_path,
    save_embedding_record,
)


def build_semantic_index(
    source_db: str | Path,
    target_db: str | Path,
    source: str = "memory_summaries",
    real_readonly: bool = False,
    write: bool = False,
    provider_name: str = "deterministic_test",
) -> dict:
    if source != "memory_summaries":
        raise ValueError("Only memory_summaries is supported")

    source_path = Path(source_db).expanduser().resolve()
    target_path = ensure_test_database_path(target_db)
    if source_path == REAL_DB_PATH.resolve() and not real_readonly:
        raise ValueError("Real source database requires --real-readonly")
    if not source_path.exists():
        raise RuntimeError("Source database does not exist")
    if not target_path.exists():
        raise RuntimeError("Target database does not exist; run migrate_semantic_memory.py first")
    _ensure_target_table_exists(target_path)

    rows = load_memory_summary_rows(source_path)
    records, empty_skipped_count = build_embedding_records_from_summaries(rows, provider_name=provider_name)

    inserted_count = 0
    duplicate_count = 0
    write_skipped_count = 0
    if write:
        for record in records:
            result = save_embedding_record(record, target_path, dry_run=False)
            if result.get("status") == "inserted":
                inserted_count += 1
            elif result.get("status") == "duplicate":
                duplicate_count += 1
            else:
                write_skipped_count += 1

    return {
        "source_count": len(rows),
        "inserted_count": inserted_count,
        "duplicate_count": duplicate_count,
        "skipped_count": empty_skipped_count + write_skipped_count,
        "provider": provider_name,
        "embedding_dim": int(records[0]["embedding_dim"]) if records else 0,
        "dry_run": not write,
        "source_table": "memory_summaries",
        "target_db_mode": "test",
    }


def load_memory_summary_rows(source_db: str | Path) -> list[dict]:
    sql = """
        SELECT
            id,
            memory_space,
            conversation_id,
            summary_type,
            summary_text,
            source_message_count,
            method
        FROM memory_summaries
        ORDER BY id ASC
    """
    conn = connect_readonly(source_db)
    try:
        conn.row_factory = sqlite3.Row
        if not _table_exists(conn, "memory_summaries"):
            return []
        return [dict(row) for row in conn.execute(sql).fetchall()]
    finally:
        conn.close()


def _ensure_target_table_exists(target_db: str | Path) -> None:
    conn = connect_readonly(target_db)
    try:
        if not _table_exists(conn, MEMORY_EMBEDDINGS_TABLE):
            raise RuntimeError("Target memory_embeddings table is missing; run migrate_semantic_memory.py first")
    finally:
        conn.close()


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        is not None
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build test semantic embeddings from memory summaries.")
    parser.add_argument("--source-db", required=True)
    parser.add_argument("--target-db", required=True)
    parser.add_argument("--source", default="memory_summaries")
    parser.add_argument("--real-readonly", action="store_true")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--provider", default="deterministic_test")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = build_semantic_index(
            args.source_db,
            args.target_db,
            source=args.source,
            real_readonly=args.real_readonly,
            write=args.write,
            provider_name=args.provider,
        )
    except (RuntimeError, ValueError) as exc:
        print(json.dumps({"status": "refused", "reason": str(exc)}, ensure_ascii=True, indent=2))
        return 2

    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
