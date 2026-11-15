from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REAL_DB_PATH = PROJECT_ROOT / "data" / ("ayunos" + "_chatgpt" + ".db")
DEFAULT_DB_PATH = REAL_DB_PATH
TEST_DB_DIR = PROJECT_ROOT / "data" / "test"
DEFAULT_MEMORY_SPACE = "whale"
SUMMARY_TYPES = {"conversation_summary", "memory_space_overview"}
METHOD = "extractive_v1"
INSUFFICIENT_SUMMARY = "\u5185\u5bb9\u4e0d\u8db3\uff0c\u65e0\u6cd5\u751f\u6210\u53ef\u9760\u6458\u8981\u3002"
PRODUCTION_WRITE_ERROR = "v3-B refuses to write to production database"
REAL_WRITE_GATE_ERROR = (
    "Refusing to write to the real database without --allow-real-write and --confirm-real-write."
)
REAL_WRITE_SPACE_ERROR = "v3-C only allows real database writes for memory_space=whale."
SUMMARY_EXISTS_ERROR = "Summary already exists. Use --replace-summary to replace it."
MISSING_SUMMARY_SCHEMA_ERROR = "memory_summaries schema is missing; run migration first"
BACKUP_DIR = PROJECT_ROOT / "backups"
BACKUP_PREFIX = "ayunos_chatgpt_before_v3c_memory_summaries"
MAX_TEXT_CHARS = 220
STOPWORDS = {
    "about",
    "after",
    "again",
    "also",
    "and",
    "because",
    "before",
    "between",
    "could",
    "from",
    "have",
    "into",
    "only",
    "that",
    "the",
    "this",
    "with",
    "would",
    "your",
    "memory",
    "summary",
}


def build_conversation_summary(
    conversation_id: str,
    memory_space: str = DEFAULT_MEMORY_SPACE,
    max_messages: int = 200,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """
    Build a summary object only. This function never writes to SQLite.
    """
    clean_conversation_id = (conversation_id or "").strip()
    if not clean_conversation_id:
        raise ValueError("conversation_id is required for conversation_summary.")

    clean_space = clean_memory_space(memory_space)
    # The optional path preserves the original read-only default while allowing
    # controlled callers to reuse this deterministic assembler on another
    # explicitly selected SQLite source.
    conn = connect_readonly(db_path or DEFAULT_DB_PATH)
    try:
        conn.row_factory = sqlite3.Row
        if not table_exists(conn, "conversations") or not table_exists(conn, "messages"):
            return base_summary(
                clean_space,
                "conversation_summary",
                conversation_id=clean_conversation_id,
                summary_text=INSUFFICIENT_SUMMARY,
            )

        conversation = fetch_conversation(conn, clean_conversation_id, clean_space)
        if conversation is None:
            return base_summary(
                clean_space,
                "conversation_summary",
                conversation_id=clean_conversation_id,
                summary_text=INSUFFICIENT_SUMMARY,
            )

        messages = fetch_messages(conn, clean_space, conversation_id=clean_conversation_id, limit=clamp_limit(max_messages, 1, 1000))
    finally:
        conn.close()

    return assemble_conversation_summary(clean_space, conversation, messages)


def build_memory_space_overview(
    memory_space: str = DEFAULT_MEMORY_SPACE,
    max_conversations: int = 50,
) -> dict[str, Any]:
    """
    Build a memory_space overview object only. This function never writes to SQLite.
    """
    clean_space = clean_memory_space(memory_space)
    conn = connect_readonly(DEFAULT_DB_PATH)
    try:
        conn.row_factory = sqlite3.Row
        if not table_exists(conn, "conversations") or not table_exists(conn, "messages"):
            return base_summary(clean_space, "memory_space_overview", summary_text=INSUFFICIENT_SUMMARY)

        conversations = fetch_conversations(conn, clean_space, clamp_limit(max_conversations, 1, 500))
        if not conversations:
            return base_summary(clean_space, "memory_space_overview", summary_text=INSUFFICIENT_SUMMARY)
        conversation_ids = [row["conversation_id"] for row in conversations if row["conversation_id"]]
        messages = fetch_messages_for_conversations(conn, clean_space, conversation_ids)
    finally:
        conn.close()

    return assemble_overview_summary(clean_space, conversations, messages)


def save_summary(
    summary: dict[str, Any],
    db_path: str | Path,
    dry_run: bool = True,
    allow_real_write: bool = False,
    confirm_real_write: bool = False,
) -> dict[str, Any]:
    """
    Persist one summary only when dry_run=False and db_path is an allowed, migrated test database.
    """
    validated = validate_summary(summary)
    path = Path(db_path).expanduser().resolve()
    result = {
        "dry_run": bool(dry_run),
        "written": False,
        "inserted_id": None,
        "backup_path": None,
        "db_path": str(path),
        "summary_type": validated["summary_type"],
        "memory_space": validated["memory_space"],
    }
    if dry_run:
        return result
    is_real_db = is_real_db_path(path)
    if is_real_db:
        gate_error = real_write_gate_error(allow_real_write, confirm_real_write)
        if gate_error:
            result["error"] = gate_error
            return result
        if validated["memory_space"] != DEFAULT_MEMORY_SPACE:
            result["error"] = REAL_WRITE_SPACE_ERROR
            return result
    elif not is_allowed_summary_write_path(path):
        result["error"] = "v3-B only writes memory summaries to test databases"
        return result

    with sqlite3.connect(path) as conn:
        if not table_exists(conn, "memory_summaries"):
            result["error"] = MISSING_SUMMARY_SCHEMA_ERROR
            return result
        if is_real_db and summary_exists(conn, validated):
            result["error"] = SUMMARY_EXISTS_ERROR
            return result
        if is_real_db:
            backup_path = backup_database(path)
            result["backup_path"] = str(backup_path)
        cursor = conn.execute(
            """
            INSERT INTO memory_summaries (
                memory_space, conversation_id, summary_type, summary_text, keywords_json,
                source_message_count, source_conversation_count, method, model_name,
                created_at, updated_at, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                validated["memory_space"],
                validated.get("conversation_id"),
                validated["summary_type"],
                validated["summary_text"],
                validated.get("keywords_json"),
                int(validated.get("source_message_count") or 0),
                int(validated.get("source_conversation_count") or 0),
                validated.get("method") or METHOD,
                validated.get("model_name"),
                validated["created_at"],
                validated.get("updated_at"),
                validated.get("metadata_json"),
            ),
        )
        conn.commit()
    result["written"] = True
    result["inserted_id"] = cursor.lastrowid
    return result


def connect_readonly(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve()
    return sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True)


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


def memory_space_expr(conn: sqlite3.Connection, columns: set[str], alias: str) -> str:
    json_available = sqlite_json_extract_available(conn)
    json_expr = "NULL"
    if json_available and "metadata_json" in columns:
        json_expr = (
            f"CASE WHEN json_valid({alias}.metadata_json) "
            f"THEN NULLIF(json_extract({alias}.metadata_json, '$.memory_space'), '') ELSE NULL END"
        )
    if "memory_space" in columns:
        return f"COALESCE(NULLIF({alias}.memory_space, ''), {json_expr}, 'general')"
    if json_expr != "NULL":
        return f"COALESCE({json_expr}, 'general')"
    if "tags" in columns:
        return (
            f"COALESCE(NULLIF(CASE WHEN instr(',' || REPLACE(COALESCE({alias}.tags, ''), ';', ',') || ',', "
            "',memory_space:whale,') > 0 OR instr(',' || REPLACE(COALESCE("
            f"{alias}.tags, ''), ';', ',') || ',', ',whale,') > 0 THEN 'whale' ELSE '' END, ''), 'general')"
        )
    return "'general'"


def fetch_conversation(conn: sqlite3.Connection, conversation_id: str, memory_space: str) -> sqlite3.Row | None:
    columns = table_columns(conn, "conversations")
    space_expr = memory_space_expr(conn, columns, "c")
    return conn.execute(
        f"""
        SELECT c.*, {space_expr} AS effective_memory_space
        FROM conversations c
        WHERE c.conversation_id = ?
          AND {space_expr} = ?
        LIMIT 1
        """,
        (conversation_id, memory_space),
    ).fetchone()


def fetch_conversations(conn: sqlite3.Connection, memory_space: str, limit: int) -> list[sqlite3.Row]:
    columns = table_columns(conn, "conversations")
    space_expr = memory_space_expr(conn, columns, "c")
    if "update_time" in columns and "create_time" in columns:
        order_expr = "COALESCE(c.update_time, c.create_time, 0)"
    elif "update_time" in columns:
        order_expr = "COALESCE(c.update_time, 0)"
    elif "create_time" in columns:
        order_expr = "COALESCE(c.create_time, 0)"
    else:
        order_expr = "c.rowid"
    return conn.execute(
        f"""
        SELECT c.*, {space_expr} AS effective_memory_space
        FROM conversations c
        WHERE {space_expr} = ?
        ORDER BY {order_expr} DESC
        LIMIT ?
        """,
        (memory_space, limit),
    ).fetchall()


def fetch_messages(
    conn: sqlite3.Connection,
    memory_space: str,
    conversation_id: str,
    limit: int,
) -> list[sqlite3.Row]:
    columns = table_columns(conn, "messages")
    space_expr = memory_space_expr(conn, columns, "m")
    order_expr = message_order_expr(columns)
    return conn.execute(
        f"""
        SELECT m.*, {space_expr} AS effective_memory_space
        FROM messages m
        WHERE m.conversation_id = ?
          AND {space_expr} = ?
        ORDER BY {order_expr}
        LIMIT ?
        """,
        (conversation_id, memory_space, limit),
    ).fetchall()


def fetch_messages_for_conversations(
    conn: sqlite3.Connection,
    memory_space: str,
    conversation_ids: list[str],
) -> list[sqlite3.Row]:
    if not conversation_ids:
        return []
    columns = table_columns(conn, "messages")
    space_expr = memory_space_expr(conn, columns, "m")
    placeholders = ",".join("?" for _ in conversation_ids)
    order_expr = message_order_expr(columns)
    return conn.execute(
        f"""
        SELECT m.*, {space_expr} AS effective_memory_space
        FROM messages m
        WHERE m.conversation_id IN ({placeholders})
          AND {space_expr} = ?
        ORDER BY m.conversation_id, {order_expr}
        """,
        (*conversation_ids, memory_space),
    ).fetchall()


def message_order_expr(columns: set[str]) -> str:
    parts = []
    if "ordinal" in columns:
        parts.append("COALESCE(m.ordinal, 0)")
    if "create_time" in columns:
        parts.append("COALESCE(m.create_time, 0)")
    parts.append("m.rowid")
    return ", ".join(parts)


def assemble_conversation_summary(
    memory_space: str,
    conversation: sqlite3.Row,
    messages: list[sqlite3.Row],
) -> dict[str, Any]:
    title = conversation_value(conversation, "title") or "(untitled conversation)"
    keywords = extract_keywords([message_text(row) for row in messages] + [title])
    user_points = select_sentences(messages, role="user", limit=2)
    assistant_points = select_sentences(messages, role="assistant", limit=2, structural=True)
    if not messages:
        summary_text = INSUFFICIENT_SUMMARY
    else:
        parts = [
            f"Conversation: {title}.",
            f"memory_space={memory_space}; messages={len(messages)}.",
        ]
        if keywords:
            parts.append(f"Keywords: {', '.join(keywords[:6])}.")
        if user_points:
            parts.append("User focus: " + " / ".join(user_points))
        if assistant_points:
            parts.append("Assistant conclusions: " + " / ".join(assistant_points))
        summary_text = clip(" ".join(parts), 1200)
    return base_summary(
        memory_space,
        "conversation_summary",
        conversation_id=conversation["conversation_id"],
        summary_text=summary_text,
        keywords=keywords,
        source_message_count=len(messages),
        source_conversation_count=1,
        metadata={
            "title": title,
            "method_note": "extractive only; no LLM call",
        },
    )


def assemble_overview_summary(
    memory_space: str,
    conversations: list[sqlite3.Row],
    messages: list[sqlite3.Row],
) -> dict[str, Any]:
    titles = [conversation_value(row, "title") or "(untitled conversation)" for row in conversations]
    keywords = extract_keywords([*titles, *[message_text(row) for row in messages]])
    assistant_points = select_sentences(messages, role="assistant", limit=3, structural=True)
    if not conversations or not messages:
        summary_text = INSUFFICIENT_SUMMARY
    else:
        parts = [
            f"memory_space={memory_space} overview.",
            f"Conversations sampled: {len(conversations)}; messages sampled: {len(messages)}.",
            "Recent titles: " + "; ".join(titles[:5]) + ".",
        ]
        if keywords:
            parts.append(f"Frequent keywords: {', '.join(keywords[:8])}.")
        if assistant_points:
            parts.append("Structural conclusions: " + " / ".join(assistant_points))
        summary_text = clip(" ".join(parts), 1200)
    return base_summary(
        memory_space,
        "memory_space_overview",
        summary_text=summary_text,
        keywords=keywords,
        source_message_count=len(messages),
        source_conversation_count=len(conversations),
        metadata={
            "conversation_ids": [row["conversation_id"] for row in conversations],
            "method_note": "extractive only; no LLM call",
        },
    )


def base_summary(
    memory_space: str,
    summary_type: str,
    summary_text: str,
    conversation_id: str | None = None,
    keywords: list[str] | None = None,
    source_message_count: int = 0,
    source_conversation_count: int = 0,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    return {
        "memory_space": memory_space,
        "conversation_id": conversation_id,
        "summary_type": summary_type,
        "summary_text": summary_text,
        "keywords_json": json.dumps(keywords or [], ensure_ascii=False),
        "source_message_count": source_message_count,
        "source_conversation_count": source_conversation_count,
        "method": METHOD,
        "model_name": None,
        "created_at": now,
        "updated_at": None,
        "metadata_json": json.dumps(metadata or {}, ensure_ascii=False),
    }


def validate_summary(summary: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(summary, dict):
        raise TypeError("summary must be a dict.")
    validated = dict(summary)
    summary_type = validated.get("summary_type")
    if summary_type not in SUMMARY_TYPES:
        raise ValueError("summary_type must be conversation_summary or memory_space_overview.")
    if summary_type == "conversation_summary" and not validated.get("conversation_id"):
        raise ValueError("conversation_summary requires conversation_id.")
    validated["memory_space"] = clean_memory_space(str(validated.get("memory_space") or DEFAULT_MEMORY_SPACE))
    if not str(validated.get("summary_text") or "").strip():
        raise ValueError("summary_text is required.")
    if not validated.get("created_at"):
        validated["created_at"] = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    validated.setdefault("method", METHOD)
    validated.setdefault("keywords_json", json.dumps([], ensure_ascii=False))
    validated.setdefault("metadata_json", json.dumps({}, ensure_ascii=False))
    return validated


def ensure_summary_schema(conn: sqlite3.Connection) -> None:
    conn.execute(SUMMARY_SCHEMA_SQL)


def is_production_db_path(path: str | Path) -> bool:
    return is_real_db_path(path)


def is_real_db_path(path: str | Path) -> bool:
    resolved = Path(path).expanduser().resolve()
    if resolved == REAL_DB_PATH.resolve():
        return True
    return resolved.name.lower() == "ayunos_chatgpt.db" and not is_relative_to(resolved, TEST_DB_DIR.resolve())


def is_allowed_summary_write_path(path: str | Path) -> bool:
    resolved = Path(path).expanduser().resolve()
    if is_production_db_path(resolved):
        return False
    if resolved.suffix.lower() not in {".db", ".sqlite", ".sqlite3"}:
        return False
    if is_relative_to(resolved, TEST_DB_DIR.resolve()):
        return True
    return is_relative_to(resolved, Path(tempfile.gettempdir()).resolve())


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def real_write_gate_error(allow_real_write: bool, confirm_real_write: bool) -> str | None:
    if allow_real_write and confirm_real_write:
        return None
    return REAL_WRITE_GATE_ERROR


def backup_database(db_path: str | Path, backup_dir: str | Path | None = None) -> Path:
    source = Path(db_path).expanduser().resolve()
    target_dir = backup_directory(backup_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = target_dir / f"{BACKUP_PREFIX}_{timestamp}.db"
    suffix = 1
    while backup_path.exists():
        backup_path = target_dir / f"{BACKUP_PREFIX}_{timestamp}_{suffix}.db"
        suffix += 1
    shutil.copy2(source, backup_path)
    if not backup_path.is_file() or backup_path.stat().st_size <= 0:
        raise RuntimeError("Failed to create a valid real database backup.")
    return backup_path


def backup_directory(backup_dir: str | Path | None = None) -> Path:
    if backup_dir is not None:
        return Path(backup_dir).expanduser().resolve()
    env_dir = os.environ.get("AYUNOS_V3C_BACKUP_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    return BACKUP_DIR


def summary_exists(conn: sqlite3.Connection, summary: dict[str, Any]) -> bool:
    conversation_id = summary.get("conversation_id")
    if conversation_id is None:
        row = conn.execute(
            """
            SELECT 1
            FROM memory_summaries
            WHERE memory_space = ?
              AND conversation_id IS NULL
              AND summary_type = ?
              AND COALESCE(method, ?) = ?
            LIMIT 1
            """,
            (
                summary["memory_space"],
                summary["summary_type"],
                summary.get("method") or METHOD,
                summary.get("method") or METHOD,
            ),
        ).fetchone()
        return row is not None
    row = conn.execute(
        """
        SELECT 1
        FROM memory_summaries
        WHERE memory_space = ?
          AND conversation_id = ?
          AND summary_type = ?
          AND COALESCE(method, ?) = ?
        LIMIT 1
        """,
        (
            summary["memory_space"],
            conversation_id,
            summary["summary_type"],
            summary.get("method") or METHOD,
            summary.get("method") or METHOD,
        ),
    ).fetchone()
    return row is not None


SUMMARY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS memory_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_space TEXT NOT NULL,
    conversation_id TEXT,
    summary_type TEXT NOT NULL,
    summary_text TEXT NOT NULL,
    keywords_json TEXT,
    source_message_count INTEGER DEFAULT 0,
    source_conversation_count INTEGER DEFAULT 0,
    method TEXT DEFAULT 'extractive_v1',
    model_name TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT,
    metadata_json TEXT,
    CHECK (summary_type IN ('conversation_summary', 'memory_space_overview')),
    CHECK (summary_type != 'conversation_summary' OR conversation_id IS NOT NULL)
);
"""


def clean_memory_space(memory_space: str) -> str:
    clean_space = (memory_space or DEFAULT_MEMORY_SPACE).strip()
    if not clean_space or clean_space in {"*", "all"}:
        raise ValueError("memory_space must name one explicit memory space.")
    return clean_space


def clamp_limit(value: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = minimum
    return max(minimum, min(number, maximum))


def conversation_value(row: sqlite3.Row, key: str) -> Any:
    return row[key] if key in row.keys() else None


def message_text(row: sqlite3.Row) -> str:
    return str(row["text"] or "") if "text" in row.keys() else ""


def extract_keywords(texts: list[str], limit: int = 12) -> list[str]:
    counter: Counter[str] = Counter()
    for text in texts:
        for token in re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z][A-Za-z0-9_-]{2,}", text or ""):
            normalized = token.lower() if token.isascii() else token
            if normalized in STOPWORDS:
                continue
            counter[normalized] += 1
    return [word for word, _ in counter.most_common(limit)]


def select_sentences(
    messages: list[sqlite3.Row],
    role: str,
    limit: int,
    structural: bool = False,
) -> list[str]:
    selected: list[str] = []
    structural_needles = ("conclusion", "summary", "therefore", "\u5efa\u8bae", "\u7ed3\u8bba", "\u603b\u7ed3")
    for row in messages:
        if "role" not in row.keys() or row["role"] != role:
            continue
        text = message_text(row)
        sentences = split_sentences(text)
        if structural:
            prioritized = [
                sentence
                for sentence in sentences
                if any(needle.lower() in sentence.lower() for needle in structural_needles)
            ]
            sentences = prioritized or sentences
        for sentence in sorted(sentences, key=information_score, reverse=True):
            compact = clip(sentence, MAX_TEXT_CHARS)
            if compact and compact not in selected:
                selected.append(compact)
                break
        if len(selected) >= limit:
            break
    return selected


def split_sentences(text: str) -> list[str]:
    compact = " ".join((text or "").split())
    if not compact:
        return []
    pieces = [piece.strip() for piece in re.split(r"(?<=[.!?\u3002\uff01\uff1f\uff1b;])\s+", compact)]
    return [piece for piece in pieces if piece]


def information_score(text: str) -> int:
    tokens = re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z][A-Za-z0-9_-]{2,}|\d+", text or "")
    signal = sum(1 for token in tokens if token.lower() not in STOPWORDS)
    punctuation_bonus = sum(text.count(marker) for marker in (":", "\uff1a", ";", "\uff1b"))
    return signal + punctuation_bonus


def clip(text: str, limit: int) -> str:
    compact = " ".join((text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."
