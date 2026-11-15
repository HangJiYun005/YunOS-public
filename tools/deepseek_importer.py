from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_IMPORT_DIR = PROJECT_ROOT / "imports" / "deepseek"
DEFAULT_JSON_PATH = DEFAULT_IMPORT_DIR / "raw_deepseek_current_chat.json"
DEFAULT_MD_PATH = DEFAULT_IMPORT_DIR / "raw_deepseek_current_chat.md"
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / ("ayunos" + "_chatgpt" + ".db")
DEFAULT_REPORT_PATH = PROJECT_ROOT / "logs" / "deepseek_import_report.md"
SOURCE_PLATFORM = "deepseek"
MEMORY_SPACE = "whale"


@dataclass(frozen=True)
class ImportResult:
    import_path: Path
    db_path: Path
    report_path: Path
    conversation_id: str
    total_messages: int
    user_messages: int
    assistant_messages: int
    inserted_messages: int
    skipped_messages: int
    search_connected: bool


def iso_to_timestamp(value: str | None) -> float | None:
    if not value:
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def load_deepseek_json(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("DeepSeek JSON export must be a JSON object")
    if raw.get("source_platform") not in {None, SOURCE_PLATFORM}:
        raise ValueError("DeepSeek JSON source_platform is not deepseek")
    messages = raw.get("messages")
    if not isinstance(messages, list):
        raise ValueError("DeepSeek JSON export must contain a messages array")
    return raw


def validate_message(raw: Any, position: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"Message {position} must be an object")
    role = raw.get("role")
    if role not in {"user", "assistant"}:
        raise ValueError(f"Message {position} has unsupported role: {role!r}")
    content = raw.get("content")
    if not isinstance(content, str):
        raise ValueError(f"Message {position} content must be a string")
    if "index" not in raw and not (raw.get("message_id") or raw.get("source_message_id") or raw.get("id")):
        raise ValueError(f"Message {position} must include index or message_id")
    return raw


def table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'virtual table') AND name = ?",
        (table_name,),
    ).fetchone()
    if not exists:
        return set()
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})")}


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        PRAGMA journal_mode=WAL;

        CREATE TABLE IF NOT EXISTS conversations (
            conversation_id TEXT PRIMARY KEY,
            title TEXT,
            create_time REAL,
            create_time_iso TEXT,
            update_time REAL,
            update_time_iso TEXT,
            source_file TEXT,
            default_model_slug TEXT,
            is_archived TEXT,
            is_starred TEXT,
            message_count INTEGER,
            user_message_count INTEGER,
            assistant_message_count INTEGER,
            char_count INTEGER,
            tags TEXT,
            importance_score INTEGER,
            metadata_json TEXT,
            memory_space TEXT DEFAULT 'general'
        );

        CREATE TABLE IF NOT EXISTS messages (
            message_id TEXT,
            node_id TEXT,
            conversation_id TEXT,
            conversation_title TEXT,
            ordinal INTEGER,
            parent_id TEXT,
            children_json TEXT,
            role TEXT,
            author_name TEXT,
            create_time REAL,
            create_time_iso TEXT,
            update_time REAL,
            update_time_iso TEXT,
            content_type TEXT,
            text TEXT,
            status TEXT,
            weight REAL,
            metadata_json TEXT,
            memory_space TEXT DEFAULT 'general'
        );
        """
    )
    for table_name in ("conversations", "messages"):
        if "source_platform" not in table_columns(conn, table_name):
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN source_platform TEXT")
        if "memory_space" not in table_columns(conn, table_name):
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN memory_space TEXT DEFAULT 'general'")
    if "metadata_json" not in table_columns(conn, "conversations"):
        conn.execute("ALTER TABLE conversations ADD COLUMN metadata_json TEXT")

    conn.executescript(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
            conversation_title,
            role,
            text,
            content='messages',
            content_rowid='rowid'
        );

        CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
          INSERT INTO messages_fts(rowid, conversation_title, role, text)
          VALUES (new.rowid, new.conversation_title, new.role, new.text);
        END;

        CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, ordinal);
        CREATE INDEX IF NOT EXISTS idx_messages_message_id ON messages(conversation_id, message_id);
        CREATE INDEX IF NOT EXISTS idx_messages_role_source ON messages(role, source_platform);
        CREATE INDEX IF NOT EXISTS idx_messages_memory_space ON messages(memory_space);
        CREATE INDEX IF NOT EXISTS idx_conversations_source ON conversations(source_platform);
        CREATE INDEX IF NOT EXISTS idx_conversations_memory_space ON conversations(memory_space);
        """
    )


def message_exists(conn: sqlite3.Connection, conversation_id: str, index_value: Any, message_id: str) -> bool:
    if index_value is not None:
        row = conn.execute(
            "SELECT 1 FROM messages WHERE conversation_id = ? AND ordinal = ? LIMIT 1",
            (conversation_id, index_value),
        ).fetchone()
        if row:
            return True
    row = conn.execute(
        "SELECT 1 FROM messages WHERE conversation_id = ? AND message_id = ? LIMIT 1",
        (conversation_id, message_id),
    ).fetchone()
    return row is not None


def refresh_conversation_summary(
    conn: sqlite3.Connection,
    conversation_id: str,
    title: str,
    import_path: Path,
    exported_at: str,
) -> None:
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS message_count,
            SUM(CASE WHEN role = 'user' THEN 1 ELSE 0 END) AS user_message_count,
            SUM(CASE WHEN role = 'assistant' THEN 1 ELSE 0 END) AS assistant_message_count,
            SUM(LENGTH(COALESCE(text, ''))) AS char_count,
            MIN(create_time) AS create_time,
            MIN(create_time_iso) AS create_time_iso,
            MAX(create_time) AS update_time,
            MAX(create_time_iso) AS update_time_iso
        FROM messages
        WHERE conversation_id = ?
        """,
        (conversation_id,),
    ).fetchone()
    conn.execute(
        """
        INSERT INTO conversations (
            conversation_id, title, create_time, create_time_iso, update_time, update_time_iso,
            source_file, default_model_slug, is_archived, is_starred, message_count,
            user_message_count, assistant_message_count, char_count, tags, importance_score,
            source_platform, metadata_json, memory_space
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(conversation_id) DO UPDATE SET
            title = excluded.title,
            update_time = excluded.update_time,
            update_time_iso = excluded.update_time_iso,
            source_file = excluded.source_file,
            message_count = excluded.message_count,
            user_message_count = excluded.user_message_count,
            assistant_message_count = excluded.assistant_message_count,
            char_count = excluded.char_count,
            source_platform = excluded.source_platform,
            metadata_json = excluded.metadata_json,
            memory_space = excluded.memory_space
        """,
        (
            conversation_id,
            title,
            row["create_time"],
            row["create_time_iso"] or exported_at,
            row["update_time"],
            row["update_time_iso"] or exported_at,
            str(import_path),
            "deepseek",
            "",
            "",
            int(row["message_count"] or 0),
            int(row["user_message_count"] or 0),
            int(row["assistant_message_count"] or 0),
            int(row["char_count"] or 0),
            "deepseek",
            0,
            SOURCE_PLATFORM,
            json.dumps(
                {
                    "source_platform": SOURCE_PLATFORM,
                    "memory_space": MEMORY_SPACE,
                    "exported_at": exported_at,
                },
                ensure_ascii=False,
            ),
            MEMORY_SPACE,
        ),
    )


def search_connected(conn: sqlite3.Connection, conversation_id: str, messages: list[dict[str, Any]]) -> bool:
    sample = next((m["content"].strip()[:24] for m in messages if m["content"].strip()), "")
    if not sample:
        return False
    row = conn.execute(
        """
        SELECT 1
        FROM messages
        WHERE conversation_id = ?
          AND role IN ('user', 'assistant')
          AND source_platform = ?
          AND memory_space = ?
          AND text LIKE ?
        LIMIT 1
        """,
        (conversation_id, SOURCE_PLATFORM, MEMORY_SPACE, f"%{sample}%"),
    ).fetchone()
    return row is not None


def write_report(result: ImportResult) -> None:
    result.report_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# DeepSeek 导入报告",
        "",
        f"- 导入文件路径：{result.import_path}",
        f"- source_platform：{SOURCE_PLATFORM}",
        f"- conversation_id：{result.conversation_id}",
        f"- 消息总数：{result.total_messages}",
        f"- user 数量：{result.user_messages}",
        f"- assistant 数量：{result.assistant_messages}",
        f"- 成功写入数：{result.inserted_messages}",
        f"- 跳过数：{result.skipped_messages}",
        f"- 数据库写入位置：{result.db_path}",
        f"- 是否成功接入 YunOS 搜索系统：{'是' if result.search_connected else '否'}",
        "",
        "## 验收字段",
        "",
        "- content：写入 messages.text，可被正文关键词检索。",
        "- role：写入 messages.role，可按 user / assistant 区分。",
        "- source_platform：写入 conversations/messages.source_platform，值为 deepseek。",
        "- memory_space：写入 metadata_json.memory_space，值为 whale。",
        "- 去重规则：优先使用 conversation_id + index；同时检查 conversation_id + message_id。",
    ]
    result.report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def import_deepseek_export(import_path: Path, db_path: Path, report_path: Path = DEFAULT_REPORT_PATH) -> ImportResult:
    payload = load_deepseek_json(import_path)
    conversation_id = str(payload.get("conversation_id") or "").strip()
    if not conversation_id:
        raise ValueError("DeepSeek JSON export is missing conversation_id")
    title = str(payload.get("title") or "DeepSeek conversation")
    exported_at = str(payload.get("exported_at") or "")
    messages = [validate_message(raw, idx) for idx, raw in enumerate(payload["messages"])]
    user_messages = sum(1 for message in messages if message["role"] == "user")
    assistant_messages = sum(1 for message in messages if message["role"] == "assistant")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    inserted = 0
    skipped = 0
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        ensure_schema(conn)
        for position, message in enumerate(messages):
            index_value = message.get("index")
            message_id = str(
                message.get("message_id")
                or message.get("source_message_id")
                or message.get("id")
                or f"{conversation_id}:{index_value if index_value is not None else position}"
            )
            if message_exists(conn, conversation_id, index_value, message_id):
                skipped += 1
                continue
            created_at = str(message.get("created_at") or "")
            metadata = {
                "source_platform": SOURCE_PLATFORM,
                "memory_space": MEMORY_SPACE,
                "source_message_id": message.get("source_message_id"),
                "raw_role": message.get("raw_role"),
                "raw_role_evidence": message.get("raw_role_evidence"),
                "raw_json": message.get("raw_json"),
            }
            conn.execute(
                """
                INSERT INTO messages (
                    message_id, node_id, conversation_id, conversation_title, ordinal, parent_id,
                    children_json, role, author_name, create_time, create_time_iso, update_time,
                    update_time_iso, content_type, text, status, weight, metadata_json, source_platform,
                    memory_space
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    message_id,
                    conversation_id,
                    title,
                    index_value if index_value is not None else position,
                    message.get("parent_id"),
                    "[]",
                    message["role"],
                    "",
                    iso_to_timestamp(created_at),
                    created_at,
                    iso_to_timestamp(created_at),
                    created_at,
                    "text",
                    message["content"],
                    message.get("status"),
                    None,
                    json.dumps(metadata, ensure_ascii=False),
                    SOURCE_PLATFORM,
                    MEMORY_SPACE,
                ),
            )
            inserted += 1
        refresh_conversation_summary(conn, conversation_id, title, import_path, exported_at)
        connected = search_connected(conn, conversation_id, messages)
        conn.commit()

    result = ImportResult(
        import_path=import_path,
        db_path=db_path,
        report_path=report_path,
        conversation_id=conversation_id,
        total_messages=len(messages),
        user_messages=user_messages,
        assistant_messages=assistant_messages,
        inserted_messages=inserted,
        skipped_messages=skipped,
        search_connected=connected,
    )
    write_report(result)
    return result


def choose_import_path() -> Path:
    if DEFAULT_JSON_PATH.exists():
        return DEFAULT_JSON_PATH
    if DEFAULT_MD_PATH.exists():
        raise FileNotFoundError("DeepSeek JSON export is missing; MD fallback is not implemented as structured input")
    raise FileNotFoundError(f"DeepSeek export not found: {DEFAULT_JSON_PATH}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import a structured DeepSeek JSON conversation into AyunOS SQLite.")
    parser.add_argument("--input", default="", help="DeepSeek JSON export path. Defaults to ignored imports/deepseek/ path.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="AyunOS SQLite database path.")
    parser.add_argument("--report", default=str(DEFAULT_REPORT_PATH), help="Import report Markdown path.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    import_path = Path(args.input).expanduser().resolve() if args.input else choose_import_path()
    result = import_deepseek_export(import_path, Path(args.db).expanduser().resolve(), Path(args.report).expanduser().resolve())
    print(f"DeepSeek import complete: total={result.total_messages}, inserted={result.inserted_messages}, skipped={result.skipped_messages}")
    print(f"Report: {result.report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
