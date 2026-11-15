from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3

from memory.ingestion_migration_rehearsal import create_consistent_backup
from memory.ingestion_migrations import (
    MigrationPlan,
    apply_migration_to_clone,
    build_migration_plan,
    inspect_schema,
)


def create_legacy_database(path: Path, *, with_rows: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(
            """
            PRAGMA journal_mode=DELETE;
            CREATE TABLE conversations(
                conversation_id TEXT PRIMARY KEY,
                title TEXT,
                create_time REAL,
                create_time_iso TEXT,
                update_time REAL,
                update_time_iso TEXT,
                source_file TEXT,
                default_model_slug TEXT,
                is_archived INTEGER,
                is_starred INTEGER,
                message_count INTEGER,
                user_message_count INTEGER,
                assistant_message_count INTEGER,
                char_count INTEGER,
                tags TEXT,
                importance_score REAL,
                source_platform TEXT,
                metadata_json TEXT,
                memory_space TEXT DEFAULT 'general'
            );
            CREATE TABLE messages(
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
                source_platform TEXT,
                memory_space TEXT DEFAULT 'general'
            );
            CREATE TABLE assets(
                asset_file TEXT PRIMARY KEY,
                original_name TEXT,
                size_bytes INTEGER
            );
            CREATE VIRTUAL TABLE messages_fts USING fts5(
                conversation_title, role, text,
                content='messages', content_rowid='rowid'
            );
            CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
              INSERT INTO messages_fts(rowid,conversation_title,role,text)
              VALUES(new.rowid,new.conversation_title,new.role,new.text);
            END;
            CREATE INDEX idx_conversations_update ON conversations(update_time);
            CREATE INDEX idx_conversations_source ON conversations(source_platform);
            CREATE INDEX idx_messages_conversation ON messages(conversation_id);
            CREATE INDEX idx_messages_message_id ON messages(message_id);
            """
        )
        if with_rows:
            conn.execute(
                "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "fixture-conversation-1", "Synthetic fixture", 1.0,
                    "2026-07-28T00:00:00Z", 2.0, "2026-07-28T00:01:00Z",
                    "fixture.json", "fixture-model", 0, 0, 2, 1, 1, 24,
                    "", 0.0, "chatgpt", '{"fixture":true}', "general",
                ),
            )
            conn.executemany(
                "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    (
                        "fixture-message-1", "node-1", "fixture-conversation-1",
                        "Synthetic fixture", 1, None, "[]", "user", "user",
                        1.0, "2026-07-28T00:00:00Z", None, None, "text",
                        "synthetic user text", "finished", 1.0,
                        '{"attachments":[{"kind":"synthetic"}]}',
                        "chatgpt", "general",
                    ),
                    (
                        "fixture-message-2", "node-2", "fixture-conversation-1",
                        "Synthetic fixture", 2, "node-1", "[]", "assistant",
                        "assistant", 2.0, "2026-07-28T00:01:00Z", None, None,
                        "text", "synthetic assistant text", "finished", 1.0,
                        '{"model_slug":"fixture-model"}', "chatgpt", "general",
                    ),
                ),
            )
        conn.commit()
    return path


def clone_database(source: Path, target: Path) -> Path:
    create_consistent_backup(source, target)
    return target


def migrate_database(path: Path, rehearsal_id: str = "rehearsal_test_001") -> MigrationPlan:
    plan = build_migration_plan(inspect_schema(path))
    apply_migration_to_clone(plan, path, rehearsal_id=rehearsal_id)
    return plan
