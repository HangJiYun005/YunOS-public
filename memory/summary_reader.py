from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import Any


SUMMARY_TYPES = {"conversation_summary", "memory_space_overview"}
SUMMARY_FIELDS = (
    "id",
    "memory_space",
    "conversation_id",
    "summary_type",
    "summary_text",
    "keywords_json",
    "source_message_count",
    "source_conversation_count",
    "method",
    "model_name",
    "created_at",
    "updated_at",
)


def list_summaries(
    db_path: str | Path,
    memory_space: str | None = None,
    summary_type: str | None = None,
    conversation_id: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """
    Read memory_summaries without writing. Missing table returns an empty list.
    """
    clean_limit = clamp_limit(limit)
    with connect_readonly(db_path) as conn:
        conn.row_factory = sqlite3.Row
        if not table_exists(conn, "memory_summaries"):
            return []
        where_parts: list[str] = []
        params: list[str] = []
        if memory_space:
            where_parts.append("memory_space = ?")
            params.append(memory_space)
        if summary_type:
            where_parts.append("summary_type = ?")
            params.append(summary_type)
        if conversation_id:
            where_parts.append("conversation_id = ?")
            params.append(conversation_id)
        where_sql = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
        rows = conn.execute(
            f"""
            SELECT {", ".join(SUMMARY_FIELDS)}
            FROM memory_summaries
            {where_sql}
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            [*params, clean_limit],
        ).fetchall()
    return [dict(row) for row in rows]


def get_summary_count(db_path: str | Path, memory_space: str | None = None) -> int:
    """
    Count memory_summaries without writing. Missing table returns 0.
    """
    with connect_readonly(db_path) as conn:
        if not table_exists(conn, "memory_summaries"):
            return 0
        if memory_space:
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM memory_summaries WHERE memory_space = ?",
                    (memory_space,),
                ).fetchone()[0]
            )
        return int(conn.execute("SELECT COUNT(*) FROM memory_summaries").fetchone()[0])


def connect_readonly(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path).expanduser().resolve()
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
            (name,),
        ).fetchone()
        is not None
    )


def clamp_limit(limit: int) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        value = 20
    return max(1, min(value, 100))
