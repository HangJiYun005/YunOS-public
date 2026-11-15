from __future__ import annotations

import argparse
from pathlib import Path
import sqlite3


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "test" / "ayunos_memory_test.db"


MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS conversation_tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    tag TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversation_marks (
    conversation_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    note TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS message_marks (
    message_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    note TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_conversation_tags_conversation_id
    ON conversation_tags(conversation_id);

CREATE INDEX IF NOT EXISTS idx_conversation_tags_tag
    ON conversation_tags(tag);

CREATE INDEX IF NOT EXISTS idx_conversation_marks_status
    ON conversation_marks(status);

CREATE INDEX IF NOT EXISTS idx_message_marks_status
    ON message_marks(status);
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Add local-only mark/tag tables to the memory database.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite database path to migrate.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    db_path = Path(args.db).expanduser().resolve()

    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")

    with sqlite3.connect(db_path) as conn:
        conn.executescript(MIGRATION_SQL)
        conn.commit()

    print(f"Migration complete: {db_path}")
    print("Tables ensured: conversation_tags, conversation_marks, message_marks")
    print(
        "Indexes ensured: idx_conversation_tags_conversation_id, "
        "idx_conversation_tags_tag, idx_conversation_marks_status, "
        "idx_message_marks_status"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
