from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "test" / "ayunos_memory_test.db"
ALLOWED_STATUSES = {"keep", "trash", "project", "study", "writing", "life", "tech", "review"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local command-line memory browser.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite database path.")
    return parser.parse_args()


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def clip(text: str | None, limit: int) -> tuple[str, bool]:
    value = text or ""
    if len(value) <= limit:
        return value, False
    return value[:limit] + "\n...[truncated; type full when prompted to view complete content]", True


def compact(text: str | None, limit: int = 200) -> str:
    value = " ".join((text or "").split())
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def print_conversation_rows(rows) -> None:
    if not rows:
        print("No conversations found.")
        return
    for idx, row in enumerate(rows, start=1):
        print(f"[{idx}] {row['title']}")
        print(f"    update_time_iso: {row['update_time_iso']}")
        print(f"    message_count: {row['message_count']}")
        print(f"    conversation_id: {row['conversation_id']}")


def recent_conversations(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT title, update_time_iso, message_count, conversation_id
        FROM conversations
        ORDER BY update_time DESC
        LIMIT 20
        """
    ).fetchall()
    print_conversation_rows(rows)


def search_titles(conn: sqlite3.Connection) -> None:
    keyword = input("Title keyword: ").strip()
    if not keyword:
        print("Empty keyword.")
        return
    rows = conn.execute(
        """
        SELECT title, update_time_iso, message_count, conversation_id
        FROM conversations
        WHERE title LIKE ?
        ORDER BY update_time DESC
        LIMIT 20
        """,
        (f"%{keyword}%",),
    ).fetchall()
    print_conversation_rows(rows)


def search_messages(conn: sqlite3.Connection) -> None:
    keyword = input("Message keyword: ").strip()
    if not keyword:
        print("Empty keyword.")
        return
    rows = conn.execute(
        """
        SELECT conversation_title, role, create_time_iso, text, conversation_id, message_id
        FROM messages
        WHERE text LIKE ?
        ORDER BY create_time DESC
        LIMIT 20
        """,
        (f"%{keyword}%",),
    ).fetchall()
    if not rows:
        print("No messages found.")
        return
    for idx, row in enumerate(rows, start=1):
        print(f"[{idx}] {row['conversation_title']}")
        print(f"    role: {row['role']}")
        print(f"    create_time_iso: {row['create_time_iso']}")
        print(f"    snippet: {compact(row['text'])}")
        print(f"    conversation_id: {row['conversation_id']}")
        print(f"    message_id: {row['message_id']}")


def view_conversation(conn: sqlite3.Connection) -> None:
    conversation_id = input("Conversation id: ").strip()
    if not conversation_id:
        print("Empty conversation id.")
        return
    title_row = conn.execute(
        "SELECT title, update_time_iso, message_count FROM conversations WHERE conversation_id = ?",
        (conversation_id,),
    ).fetchone()
    if not title_row:
        print("Conversation not found.")
        return
    print(f"Title: {title_row['title']}")
    print(f"Updated: {title_row['update_time_iso']}")
    print(f"Messages: {title_row['message_count']}")

    rows = conn.execute(
        """
        SELECT role, create_time_iso, text
        FROM messages
        WHERE conversation_id = ?
        ORDER BY ordinal ASC, create_time ASC
        """,
        (conversation_id,),
    ).fetchall()
    for idx, row in enumerate(rows, start=1):
        content, truncated = clip(row["text"], 800)
        print(f"\n[{idx}] {row['role']} | {row['create_time_iso']}")
        print(content)
        if truncated:
            choice = input("Type full to view complete content, or press Enter to continue: ").strip().lower()
            if choice == "full":
                print(row["text"] or "")


def add_tags(conn: sqlite3.Connection) -> None:
    conversation_id = input("Conversation id: ").strip()
    tags_text = input("Tags, separated by comma: ").strip()
    tags = [tag.strip() for tag in tags_text.replace("，", ",").split(",") if tag.strip()]
    if not conversation_id or not tags:
        print("Conversation id and at least one tag are required.")
        return
    exists = conn.execute(
        "SELECT 1 FROM conversations WHERE conversation_id = ?",
        (conversation_id,),
    ).fetchone()
    if not exists:
        print("Conversation not found.")
        return
    created_at = now_iso()
    conn.executemany(
        "INSERT INTO conversation_tags (conversation_id, tag, created_at) VALUES (?, ?, ?)",
        [(conversation_id, tag, created_at) for tag in tags],
    )
    conn.commit()
    print(f"Added tags: {', '.join(tags)}")


def mark_conversation(conn: sqlite3.Connection) -> None:
    conversation_id = input("Conversation id: ").strip()
    status = input(f"Status {sorted(ALLOWED_STATUSES)}: ").strip()
    note = input("Note (optional): ").strip()
    if status not in ALLOWED_STATUSES:
        print("Invalid status.")
        return
    exists = conn.execute(
        "SELECT 1 FROM conversations WHERE conversation_id = ?",
        (conversation_id,),
    ).fetchone()
    if not exists:
        print("Conversation not found.")
        return
    conn.execute(
        """
        INSERT INTO conversation_marks (conversation_id, status, note, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(conversation_id) DO UPDATE SET
            status = excluded.status,
            note = excluded.note,
            updated_at = excluded.updated_at
        """,
        (conversation_id, status, note or None, now_iso()),
    )
    conn.commit()
    print("Conversation mark saved.")


def list_marked(conn: sqlite3.Connection) -> None:
    status = input("Status filter, or Enter for all: ").strip()
    params: tuple[str, ...] = ()
    where = ""
    if status:
        where = "WHERE cm.status = ?"
        params = (status,)
    rows = conn.execute(
        f"""
        SELECT cm.status, c.title, cm.note, cm.updated_at, cm.conversation_id
        FROM conversation_marks cm
        JOIN conversations c ON c.conversation_id = cm.conversation_id
        {where}
        ORDER BY cm.updated_at DESC
        LIMIT 50
        """,
        params,
    ).fetchall()
    if not rows:
        print("No marked conversations found.")
        return
    for idx, row in enumerate(rows, start=1):
        print(f"[{idx}] {row['status']} | {row['title']}")
        print(f"    note: {row['note'] or ''}")
        print(f"    updated_at: {row['updated_at']}")
        print(f"    conversation_id: {row['conversation_id']}")


def list_by_tag(conn: sqlite3.Connection) -> None:
    tag = input("Tag: ").strip()
    if not tag:
        print("Empty tag.")
        return
    rows = conn.execute(
        """
        SELECT c.title, c.update_time_iso, c.message_count, c.conversation_id
        FROM conversation_tags ct
        JOIN conversations c ON c.conversation_id = ct.conversation_id
        WHERE ct.tag = ?
        ORDER BY ct.created_at DESC
        LIMIT 50
        """,
        (tag,),
    ).fetchall()
    print_conversation_rows(rows)


def show_menu() -> None:
    print()
    print("AyunOS Memory Browser")
    print("1. 查看最近对话")
    print("2. 搜索对话标题")
    print("3. 搜索消息正文")
    print("4. 查看某个对话详情")
    print("5. 给对话打标签")
    print("6. 标记对话状态")
    print("7. 查看已标记对话")
    print("8. 查看某标签下的对话")
    print("9. 退出")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    args = parse_args()
    db_path = Path(args.db).expanduser().resolve()
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")

    actions = {
        "1": recent_conversations,
        "2": search_titles,
        "3": search_messages,
        "4": view_conversation,
        "5": add_tags,
        "6": mark_conversation,
        "7": list_marked,
        "8": list_by_tag,
    }
    with connect(db_path) as conn:
        while True:
            show_menu()
            choice = input("Choose: ").strip()
            if choice == "9":
                print("Bye.")
                return 0
            action = actions.get(choice)
            if not action:
                print("Invalid choice.")
                continue
            action(conn)


if __name__ == "__main__":
    raise SystemExit(main())
