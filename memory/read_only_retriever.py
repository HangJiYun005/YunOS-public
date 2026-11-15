from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / ("ayunos" + "_chatgpt" + ".db")
DEFAULT_MEMORY_SPACE = "whale"
MAX_SNIPPET_CHARS = 240


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'virtual table') AND name = ?",
            (name,),
        ).fetchone()
        is not None
    )


def table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    if not table_exists(conn, table_name):
        return set()
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})")}


def sqlite_json_extract_available(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("SELECT json_extract(?, '$.memory_space')", ('{"memory_space":"whale"}',)).fetchone()
    except sqlite3.DatabaseError:
        return False
    return True


def source_expr(columns: set[str], alias: str) -> str:
    if "source_platform" in columns:
        return f"COALESCE(NULLIF({alias}.source_platform, ''), 'chatgpt')"
    return "'chatgpt'"


def json_memory_space_expr(columns: set[str], alias: str) -> str:
    if "metadata_json" in columns:
        return (
            f"CASE WHEN json_valid({alias}.metadata_json) "
            f"THEN NULLIF(json_extract({alias}.metadata_json, '$.memory_space'), '') ELSE NULL END"
        )
    if "metadata" in columns:
        return (
            f"CASE WHEN json_valid({alias}.metadata) "
            f"THEN NULLIF(json_extract({alias}.metadata, '$.memory_space'), '') ELSE NULL END"
        )
    return "NULL"


def memory_space_expr(
    conn: sqlite3.Connection,
    columns: set[str],
    alias: str,
    conversation_alias: str,
) -> tuple[str, list[str]]:
    json_available = sqlite_json_extract_available(conn)
    if "memory_space" in columns:
        fallback = json_memory_space_expr(columns, alias) if json_available else "NULL"
        return f"COALESCE(NULLIF({alias}.memory_space, ''), {fallback}, 'general')", []
    if json_available and ("metadata_json" in columns or "metadata" in columns):
        return f"COALESCE({json_memory_space_expr(columns, alias)}, 'general')", []
    if "tags" in columns:
        return (
            f"""
            COALESCE(
              NULLIF(CASE
                WHEN instr(',' || REPLACE(COALESCE({alias}.tags, ''), ';', ',') || ',', ',memory_space:whale,') > 0
                  OR instr(',' || REPLACE(COALESCE({alias}.tags, ''), ';', ',') || ',', ',whale,') > 0
                THEN 'whale'
                ELSE ''
              END, ''),
              'general'
            )
            """,
            [],
        )
    if table_exists(conn, "conversation_tags"):
        return (
            f"""
            COALESCE(
              NULLIF(CASE
                WHEN EXISTS (
                    SELECT 1 FROM conversation_tags ct
                    WHERE ct.conversation_id = {conversation_alias}.conversation_id
                      AND ct.tag IN ('memory_space:whale', 'whale')
                )
                THEN 'whale'
                ELSE ''
              END, ''),
              'general'
            )
            """,
            [],
        )
    if table_exists(conn, "conversation_memory_space"):
        return (
            f"""
            COALESCE((
                SELECT cms.memory_space
                FROM conversation_memory_space cms
                WHERE cms.conversation_id = {conversation_alias}.conversation_id
                LIMIT 1
            ), 'general')
            """,
            [],
        )
    return "'general'", []


def user_filter_clause(columns: set[str], alias: str, user_id: str | None) -> tuple[str, list[str]]:
    if "user_id" not in columns:
        return "", []
    if not user_id:
        return " AND 1 = 0", []
    return f" AND {alias}.user_id = ?", [user_id]


def clip(text: str | None, limit: int = MAX_SNIPPET_CHARS) -> str:
    compact = " ".join((text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def text_snippet(text: str | None, query: str, limit: int = MAX_SNIPPET_CHARS) -> str:
    source = " ".join((text or "").split())
    if not source:
        return ""
    pos = source.lower().find(query.lower())
    if pos < 0:
        return clip(source, limit)
    start = max(0, pos - 60)
    end = min(len(source), pos + len(query) + 140)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(source) else ""
    return clip(prefix + source[start:end] + suffix, limit)


def query_terms(query: str) -> list[str]:
    return [term for term in query.split() if term]


def like_terms_sql(expr: str, terms: list[str]) -> tuple[str, list[str]]:
    return " AND ".join(f"{expr} LIKE ?" for _ in terms), [f"%{term}%" for term in terms]


def _numeric_sort_time(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _relevance_score(text: str | None, title: str | None, query: str, terms: list[str]) -> int:
    body = " ".join((text or "").split()).casefold()
    heading = " ".join((title or "").split()).casefold()
    phrase = query.casefold()
    normalized_terms = [term.casefold() for term in terms]
    phrase_count = body.count(phrase)
    body_term_count = sum(body.count(term) for term in normalized_terms)
    title_phrase_count = heading.count(phrase)
    title_term_count = sum(heading.count(term) for term in normalized_terms)
    return (
        phrase_count * 1000
        + body_term_count * 100
        + title_phrase_count * 10
        + title_term_count
    )


def search_all_memory_conversations(
    query: str,
    *,
    sort: str = "relevance",
    offset: int = 0,
    limit: int = 20,
    db_path: Path | str = DEFAULT_DB_PATH,
    source_platform: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Search message bodies across memory spaces and group hits by conversation.

    This is intentionally separate from ``retrieve``: ordinary read-only search
    may span every memory space, while persona/context retrieval remains
    fail-closed to one explicit space.
    """
    clean_query = query.strip()
    clean_sort = sort.strip().lower()
    if clean_sort not in {"relevance", "latest"}:
        raise ValueError("sort must be relevance or latest.")
    if offset < 0:
        raise ValueError("offset must be at least 0.")
    if limit < 1 or limit > 20:
        raise ValueError("limit must be between 1 and 20.")
    if not clean_query:
        return {
            "items": [],
            "total_conversations": 0,
            "has_more": False,
            "next_offset": None,
            "sort": clean_sort,
            "limit": limit,
            "offset": offset,
        }
    terms = query_terms(clean_query)
    if not terms:
        return {
            "items": [],
            "total_conversations": 0,
            "has_more": False,
            "next_offset": None,
            "sort": clean_sort,
            "limit": limit,
            "offset": offset,
        }

    path = Path(db_path).expanduser().resolve()
    with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conversation_columns = table_columns(conn, "conversations")
        message_columns = table_columns(conn, "messages")
        if not conversation_columns or not message_columns:
            rows: list[sqlite3.Row] = []
        else:
            message_source_expr = source_expr(message_columns, "m")
            message_space_expr, message_space_params = memory_space_expr(conn, message_columns, "m", "c")
            msg_user_filter, msg_user_params = user_filter_clause(message_columns, "m", user_id)
            body_terms_sql, body_terms_params = like_terms_sql("COALESCE(m.text, '')", terms)
            source_filter_sql = ""
            source_filter_params: list[str] = []
            clean_source = (source_platform or "").strip().lower()
            if clean_source:
                source_filter_sql = f" AND {message_source_expr} = ?"
                source_filter_params.append(clean_source)
            rows = conn.execute(
                f"""
                SELECT
                    m.conversation_id,
                    m.message_id,
                    COALESCE(NULLIF(m.conversation_title, ''), c.title, '') AS title,
                    m.create_time_iso AS update_time_iso,
                    m.role,
                    m.text,
                    {message_source_expr} AS source_platform,
                    {message_space_expr} AS memory_space,
                    m.create_time AS sort_time
                FROM messages m
                JOIN conversations c ON c.conversation_id = m.conversation_id
                WHERE {body_terms_sql}
                  {source_filter_sql}
                  {msg_user_filter}
                """,
                (
                    *body_terms_params,
                    *message_space_params,
                    *source_filter_params,
                    *msg_user_params,
                ),
            ).fetchall()

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        title = row["title"] or ""
        text = row["text"] or ""
        snippet_query = clean_query if clean_query.casefold() in text.casefold() else terms[0]
        hit = {
            "hit_source": "message",
            "title": title,
            "update_time_iso": row["update_time_iso"] or "",
            "role": row["role"] or "",
            "snippet": text_snippet(text, snippet_query),
            "conversation_id": row["conversation_id"] or "",
            "message_id": row["message_id"] or "",
            "source_platform": row["source_platform"] or "chatgpt",
            "memory_space": row["memory_space"] or "",
            "relevance_score": _relevance_score(text, title, clean_query, terms),
            "_sort_time": _numeric_sort_time(row["sort_time"]),
        }
        grouped.setdefault(hit["conversation_id"], []).append(hit)

    def hit_key(hit: dict[str, Any]) -> tuple[Any, ...]:
        stable_ids = (hit["conversation_id"], hit["message_id"])
        if clean_sort == "latest":
            return (-hit["_sort_time"], -hit["relevance_score"], *stable_ids)
        return (-hit["relevance_score"], -hit["_sort_time"], *stable_ids)

    groups: list[dict[str, Any]] = []
    for conversation_id, hits in grouped.items():
        ordered_hits = sorted(hits, key=hit_key)
        best_sort_time = ordered_hits[0]["_sort_time"]
        for hit in ordered_hits:
            hit.pop("_sort_time", None)
        best_hit = ordered_hits[0]
        groups.append(
            {
                "conversation_id": conversation_id,
                "title": best_hit["title"],
                "best_hit": best_hit,
                "other_hits": ordered_hits[1:],
                "match_count": len(ordered_hits),
                "other_match_count": max(0, len(ordered_hits) - 1),
                "_best_sort_time": best_sort_time,
            }
        )

    def group_key(group: dict[str, Any]) -> tuple[Any, ...]:
        best_hit = group["best_hit"]
        if clean_sort == "latest":
            return (-group["_best_sort_time"], -best_hit["relevance_score"], group["conversation_id"])
        return (-best_hit["relevance_score"], -group["_best_sort_time"], group["conversation_id"])

    groups.sort(key=group_key)
    total_conversations = len(groups)
    page = groups[offset : offset + limit]
    for group in page:
        group.pop("_best_sort_time", None)
    next_offset = offset + len(page)
    has_more = next_offset < total_conversations
    return {
        "items": page,
        "total_conversations": total_conversations,
        "has_more": has_more,
        "next_offset": next_offset if has_more else None,
        "sort": clean_sort,
        "limit": limit,
        "offset": offset,
    }


def retrieve(
    query: str,
    top_k: int = 5,
    memory_space: str = DEFAULT_MEMORY_SPACE,
    use_embedding: bool = False,
    db_path: Path | str = DEFAULT_DB_PATH,
    user_id: str | None = None,
) -> list[dict[str, Any]]:
    if use_embedding:
        raise NotImplementedError("Embedding retrieval is reserved for a later version.")
    clean_query = query.strip()
    if not clean_query:
        return []
    terms = query_terms(clean_query)
    if not terms:
        return []
    clean_space = (memory_space or DEFAULT_MEMORY_SPACE).strip()
    if not clean_space or clean_space in {"*", "all"}:
        raise ValueError("memory_space must name one explicit memory space.")

    limit = max(1, min(int(top_k), 20))
    path = Path(db_path).expanduser().resolve()
    with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conversation_columns = table_columns(conn, "conversations")
        message_columns = table_columns(conn, "messages")
        if not conversation_columns or not message_columns:
            return []

        conversation_source_expr = source_expr(conversation_columns, "c")
        message_source_expr = source_expr(message_columns, "m")
        conversation_space_expr, conversation_space_params = memory_space_expr(conn, conversation_columns, "c", "c")
        message_space_expr, message_space_params = memory_space_expr(conn, message_columns, "m", "m")
        conv_user_filter, conv_user_params = user_filter_clause(conversation_columns, "c", user_id)
        msg_user_filter, msg_user_params = user_filter_clause(message_columns, "m", user_id)
        title_terms_sql, title_terms_params = like_terms_sql("c.title", terms)
        message_terms_sql, message_terms_params = like_terms_sql(
            "COALESCE(m.text, '') || ' ' || COALESCE(m.conversation_title, '')",
            terms,
        )

        title_rows = conn.execute(
            f"""
            SELECT
                'title' AS hit_source,
                c.title,
                c.update_time_iso,
                '' AS role,
                c.title AS snippet,
                c.conversation_id,
                '' AS message_id,
                {conversation_source_expr} AS source_platform,
                {conversation_space_expr} AS memory_space,
                c.update_time AS sort_time
            FROM conversations c
            WHERE {title_terms_sql}
              AND {conversation_space_expr} = ?
              {conv_user_filter}
            ORDER BY c.update_time DESC
            LIMIT ?
            """,
            (
                *title_terms_params,
                *conversation_space_params,
                clean_space,
                *conversation_space_params,
                *conv_user_params,
                limit,
            ),
        ).fetchall()

        message_rows = conn.execute(
            f"""
            SELECT
                'message' AS hit_source,
                m.conversation_title AS title,
                m.create_time_iso AS update_time_iso,
                m.role,
                m.text AS snippet,
                m.conversation_id,
                m.message_id,
                {message_source_expr} AS source_platform,
                {message_space_expr} AS memory_space,
                m.create_time AS sort_time
            FROM messages m
            WHERE {message_terms_sql}
              AND {message_space_expr} = ?
              {msg_user_filter}
            ORDER BY m.create_time DESC
            LIMIT ?
            """,
            (
                *message_terms_params,
                *message_space_params,
                clean_space,
                *message_space_params,
                *msg_user_params,
                limit,
            ),
        ).fetchall()

    rows = sorted(
        list(title_rows) + list(message_rows),
        key=lambda row: row["sort_time"] or 0,
        reverse=True,
    )[:limit]
    return [
        {
            "hit_source": row["hit_source"],
            "title": row["title"] or "",
            "update_time_iso": row["update_time_iso"] or "",
            "role": row["role"] or "",
            "snippet": text_snippet(row["snippet"], clean_query),
            "conversation_id": row["conversation_id"] or "",
            "message_id": row["message_id"] or "",
            "source_platform": row["source_platform"] or "chatgpt",
            "memory_space": row["memory_space"] or "",
        }
        for row in rows
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only whale memory retriever.")
    parser.add_argument("query", help="Search keyword.")
    parser.add_argument("--top-k", type=int, default=5, help="Maximum result rows.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite database path.")
    parser.add_argument("--memory-space", default=DEFAULT_MEMORY_SPACE, help="Explicit memory space.")
    parser.add_argument("--user-id", default="", help="Optional user id filter when the schema has user_id.")
    return parser.parse_args()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args()
    rows = retrieve(
        args.query,
        top_k=args.top_k,
        memory_space=args.memory_space,
        db_path=args.db,
        user_id=args.user_id or None,
    )
    for idx, row in enumerate(rows, start=1):
        print(f"[{idx}] {row['title']}")
        print(f"source_platform: {row['source_platform']}")
        print(f"memory_space: {row['memory_space']}")
        print(f"role: {row['role']}")
        print(f"snippet: {row['snippet']}")
        print(f"conversation_id: {row['conversation_id']}")
        print(f"message_id: {row['message_id']}")
        print()
    if not rows:
        print("No results.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
