#!/usr/bin/env python3
"""Safely append strictly newer conversations from a ChatGPT export ZIP."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from chatgpt_export_importer import (
    classify_conversation,
    estimate_importance,
    flatten_conversation,
    load_conversations,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = PROJECT_ROOT / "data" / ("ayunos" + "_chatgpt" + ".db")
DEFAULT_BACKUPS = PROJECT_ROOT / "backups"

CONVERSATION_COLUMNS = (
    "conversation_id", "title", "create_time", "create_time_iso", "update_time",
    "update_time_iso", "source_file", "default_model_slug", "is_archived",
    "is_starred", "message_count", "user_message_count", "assistant_message_count",
    "char_count", "tags", "importance_score", "source_platform", "metadata_json",
    "memory_space",
)
MESSAGE_COLUMNS = (
    "message_id", "node_id", "conversation_id", "conversation_title", "ordinal",
    "parent_id", "children_json", "role", "author_name", "create_time",
    "create_time_iso", "update_time", "update_time_iso", "content_type", "text",
    "status", "weight", "metadata_json", "source_platform", "memory_space",
)


def exact_iso(value: Any) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat(timespec="microseconds")


def ro_connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def scalar(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> Any:
    return conn.execute(sql, tuple(params)).fetchone()[0]


def db_state(conn: sqlite3.Connection) -> dict[str, Any]:
    latest = conn.execute(
        """SELECT conversation_id, create_time, create_time_iso, update_time, update_time_iso
           FROM conversations ORDER BY create_time DESC, conversation_id LIMIT 1"""
    ).fetchone()
    return {
        "conversations": scalar(conn, "SELECT COUNT(*) FROM conversations"),
        "messages": scalar(conn, "SELECT COUNT(*) FROM messages"),
        "max_create_time": scalar(conn, "SELECT MAX(create_time) FROM conversations"),
        "max_update_time": scalar(conn, "SELECT MAX(update_time) FROM conversations"),
        "latest_conversation": dict(latest) if latest else None,
    }


def required_schema(conn: sqlite3.Connection) -> None:
    for table, required in (
        ("conversations", set(CONVERSATION_COLUMNS)),
        ("messages", set(MESSAGE_COLUMNS)),
    ):
        actual = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
        missing = sorted(required - actual)
        if missing:
            raise RuntimeError(f"{table} is missing required columns: {missing}")


def protected_table_fingerprints(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    names = [
        row[0]
        for row in conn.execute(
            """SELECT name FROM sqlite_master
               WHERE type='table' AND name NOT LIKE 'sqlite_%'
                 AND name NOT IN ('conversations', 'messages')
                 AND name NOT LIKE 'messages_fts%'
               ORDER BY name"""
        )
    ]
    result: dict[str, dict[str, Any]] = {}
    for name in names:
        digest_rows: list[str] = []
        for row in conn.execute(f'SELECT * FROM "{name}"'):
            encoded = []
            for value in row:
                if isinstance(value, bytes):
                    encoded.append({"blob_sha256": hashlib.sha256(value).hexdigest()})
                else:
                    encoded.append(value)
            digest_rows.append(json.dumps(encoded, ensure_ascii=False, sort_keys=True, default=str))
        digest_rows.sort()
        digest = hashlib.sha256("\n".join(digest_rows).encode("utf-8")).hexdigest()
        result[name] = {"count": len(digest_rows), "sha256": digest}
    return result


def duplicate_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        "conversation_id_groups": scalar(
            conn,
            "SELECT COUNT(*) FROM (SELECT conversation_id FROM conversations GROUP BY conversation_id HAVING COUNT(*) > 1)",
        ),
        "message_id_groups": scalar(
            conn,
            """SELECT COUNT(*) FROM (
                   SELECT message_id FROM messages
                   WHERE message_id IS NOT NULL AND TRIM(message_id) <> ''
                   GROUP BY message_id HAVING COUNT(*) > 1
               )""",
        ),
    }


def time_range(values: Iterable[Any]) -> dict[str, Any] | None:
    numeric = [float(value) for value in values if value is not None]
    if not numeric:
        return None
    low, high = min(numeric), max(numeric)
    return {
        "earliest": low,
        "earliest_iso": exact_iso(low),
        "latest": high,
        "latest_iso": exact_iso(high),
    }


def prepare_plan(zip_path: Path, db_path: Path) -> dict[str, Any]:
    with closing(ro_connect(db_path)) as conn:
        required_schema(conn)
        before = db_state(conn)
        pre_duplicates = duplicate_counts(conn)
        if any(pre_duplicates.values()):
            raise RuntimeError(f"Existing database already has duplicate IDs: {pre_duplicates}")
        existing_conv_ids = {row[0] for row in conn.execute("SELECT conversation_id FROM conversations")}
        existing_message_ids = {
            row[0] for row in conn.execute(
                "SELECT message_id FROM messages WHERE message_id IS NOT NULL AND TRIM(message_id) <> ''"
            )
        }
        existing_fallback_keys = {
            (row[0], row[1]) for row in conn.execute(
                """SELECT conversation_id, node_id FROM messages
                   WHERE node_id IS NOT NULL AND TRIM(node_id) <> ''"""
            )
        }

    cutoff = before["max_create_time"]
    if cutoff is None:
        raise RuntimeError("Existing database has no maximum conversation create_time")

    raw_conversations = load_conversations(zip_path)
    seen_conv_ids: set[str] = set()
    seen_zip_message_ids: set[str] = set()
    overlap_conversations = 0
    overlap_messages = 0
    old_date_new_id_conversations = 0
    internal_duplicate_messages = 0
    ambiguous_new_conversations: list[str] = []
    candidate_conversations: list[dict[str, Any]] = []
    candidate_messages: list[dict[str, Any]] = []
    candidate_message_ids: set[str] = set()
    zip_message_count = 0
    zip_conv_times: list[Any] = []
    zip_message_times: list[Any] = []
    skipped_candidate_duplicate_messages = 0

    for conv in raw_conversations:
        conv_id = str(conv.get("conversation_id") or conv.get("id") or "").strip()
        if not conv_id:
            raise RuntimeError("ZIP contains a conversation without a stable conversation_id")
        if conv_id in seen_conv_ids:
            raise RuntimeError(f"ZIP contains a duplicate conversation_id: {conv_id}")
        seen_conv_ids.add(conv_id)

        create_time = conv.get("create_time")
        if create_time is not None:
            try:
                create_time = float(create_time)
            except (TypeError, ValueError) as exc:
                raise RuntimeError(f"Invalid create_time for conversation {conv_id}") from exc
            zip_conv_times.append(create_time)

        rows = flatten_conversation(conv)
        zip_message_count += len(rows)
        for row in rows:
            if row.get("create_time") is not None:
                zip_message_times.append(row["create_time"])
            message_id = str(row.get("message_id") or "").strip()
            node_id = str(row.get("node_id") or "").strip()
            if not message_id and not node_id:
                raise RuntimeError(f"Conversation {conv_id} contains a message without a reliable ID")
            effective_id = message_id or node_id
            fallback_key = (conv_id, node_id)
            if effective_id in existing_message_ids or (node_id and fallback_key in existing_fallback_keys):
                overlap_messages += 1
            if effective_id in seen_zip_message_ids:
                internal_duplicate_messages += 1
            else:
                seen_zip_message_ids.add(effective_id)

        if conv_id in existing_conv_ids:
            overlap_conversations += 1
            continue
        if create_time is None:
            ambiguous_new_conversations.append(conv_id)
            continue
        if create_time <= float(cutoff):
            old_date_new_id_conversations += 1
            continue

        full_text = "\n".join(row["text"] for row in rows if row["text"])
        title = conv.get("title") or ""
        filtered_rows: list[dict[str, Any]] = []
        for ordinal, row in enumerate(rows):
            message_id = str(row.get("message_id") or row.get("node_id") or "").strip()
            node_id = str(row.get("node_id") or "").strip()
            fallback_key = (conv_id, node_id)
            if message_id in existing_message_ids or (node_id and fallback_key in existing_fallback_keys):
                skipped_candidate_duplicate_messages += 1
                continue
            if message_id in candidate_message_ids:
                skipped_candidate_duplicate_messages += 1
                continue
            candidate_message_ids.add(message_id)
            item = dict(row)
            item.update({
                "message_id": message_id,
                "conversation_id": conv_id,
                "conversation_title": title,
                "ordinal": ordinal,
                "create_time_iso": exact_iso(row.get("create_time")),
                "update_time_iso": exact_iso(row.get("update_time")),
                "source_platform": "chatgpt",
                "memory_space": "general",
            })
            filtered_rows.append(item)

        tags = classify_conversation(title, full_text)
        conv_record = {
            "conversation_id": conv_id,
            "title": title,
            "create_time": create_time,
            "create_time_iso": exact_iso(create_time),
            "update_time": conv.get("update_time"),
            "update_time_iso": exact_iso(conv.get("update_time")),
            "source_file": conv.get("_source_file"),
            "default_model_slug": conv.get("default_model_slug"),
            "is_archived": conv.get("is_archived"),
            "is_starred": conv.get("is_starred"),
            "message_count": len(filtered_rows),
            "user_message_count": sum(1 for row in filtered_rows if row["role"] == "user"),
            "assistant_message_count": sum(1 for row in filtered_rows if row["role"] == "assistant"),
            "char_count": sum(len(row["text"]) for row in filtered_rows),
            "tags": ";".join(tags),
            "importance_score": estimate_importance(title, full_text, len(filtered_rows)),
            "source_platform": "chatgpt",
            "metadata_json": json.dumps({"import_source": "chatgpt_export"}, ensure_ascii=False),
            "memory_space": "general",
        }
        candidate_conversations.append(conv_record)
        candidate_messages.extend(filtered_rows)

    if ambiguous_new_conversations:
        raise RuntimeError(
            "New-ID conversations have no precise create_time: "
            + ", ".join(ambiguous_new_conversations[:10])
        )
    pending_conv_range = time_range(row["create_time"] for row in candidate_conversations)
    pending_msg_range = time_range(row["create_time"] for row in candidate_messages)
    date_overlap = bool(pending_conv_range and pending_conv_range["earliest"] <= float(cutoff))
    if pending_msg_range and pending_msg_range["earliest"] <= float(cutoff):
        date_overlap = True

    return {
        "zip_path": str(zip_path.resolve()),
        "db_path": str(db_path.resolve()),
        "before": before,
        "cutoff": {"value": cutoff, "iso": exact_iso(cutoff)},
        "zip": {
            "conversations": len(raw_conversations),
            "messages": zip_message_count,
            "conversation_time_range": time_range(zip_conv_times),
            "message_time_range": time_range(zip_message_times),
        },
        "overlap_conversations": overlap_conversations,
        "overlap_messages": overlap_messages,
        "zip_internal_duplicate_message_occurrences": internal_duplicate_messages,
        "old_date_new_id_conversations": old_date_new_id_conversations,
        "candidate_conversations_count": len(candidate_conversations),
        "candidate_messages_count": len(candidate_messages),
        "skipped_candidate_duplicate_messages": skipped_candidate_duplicate_messages,
        "pending_conversation_time_range": pending_conv_range,
        "pending_message_time_range": pending_msg_range,
        "date_overlap": date_overlap,
        "safe_to_import": not date_overlap,
        "candidate_conversations": candidate_conversations,
        "candidate_messages": candidate_messages,
    }


def public_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in plan.items() if not key.startswith("candidate_") or key.endswith("_count")}


def create_complete_backup(db_path: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_path = backup_dir / f"{db_path.stem}_{stamp}{db_path.suffix}"
    if backup_path.exists():
        raise FileExistsError(backup_path)
    with closing(ro_connect(db_path)) as source, closing(sqlite3.connect(backup_path)) as destination:
        destination.row_factory = sqlite3.Row
        source.backup(destination)
        if scalar(destination, "PRAGMA integrity_check") != "ok":
            raise RuntimeError("Backup integrity_check failed")
        if db_state(source) != db_state(destination):
            raise RuntimeError("Backup counts/timestamps do not match source")
    return backup_path


def insert_rows(conn: sqlite3.Connection, table: str, columns: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    placeholders = ",".join("?" for _ in columns)
    conn.executemany(
        f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})",
        [[row.get(column) for column in columns] for row in rows],
    )


def verify_state(
    conn: sqlite3.Connection,
    plan: dict[str, Any],
    protected_before: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    after = db_state(conn)
    conv_delta = after["conversations"] - plan["before"]["conversations"]
    msg_delta = after["messages"] - plan["before"]["messages"]
    duplicates = duplicate_counts(conn)
    orphans = scalar(
        conn,
        """SELECT COUNT(*) FROM messages m LEFT JOIN conversations c
           ON c.conversation_id=m.conversation_id WHERE c.conversation_id IS NULL""",
    )
    foreign_key_issues = len(conn.execute("PRAGMA foreign_key_check").fetchall())
    fts_count = scalar(conn, "SELECT COUNT(*) FROM messages_fts")
    protected_after = protected_table_fingerprints(conn)
    new_ids = [row["conversation_id"] for row in plan["candidate_conversations"]]
    if new_ids:
        placeholders = ",".join("?" for _ in new_ids)
        earliest_new = scalar(
            conn,
            f"SELECT MIN(create_time) FROM conversations WHERE conversation_id IN ({placeholders})",
            new_ids,
        )
    else:
        earliest_new = None
    checks = {
        "conversation_delta_matches": conv_delta == plan["candidate_conversations_count"],
        "message_delta_matches": msg_delta == plan["candidate_messages_count"],
        "conversation_ids_unique": duplicates["conversation_id_groups"] == 0,
        "message_ids_unique": duplicates["message_id_groups"] == 0,
        "new_earliest_strictly_after_cutoff": earliest_new is None or earliest_new > plan["cutoff"]["value"],
        "no_orphan_messages": orphans == 0,
        "foreign_key_check_ok": foreign_key_issues == 0,
        "fts_message_count_matches": fts_count == after["messages"],
        "protected_tables_unchanged": protected_after == protected_before,
    }
    return {
        "after": after,
        "conversation_delta": conv_delta,
        "message_delta": msg_delta,
        "earliest_new_create_time": earliest_new,
        "earliest_new_create_time_iso": exact_iso(earliest_new),
        "duplicate_groups": duplicates,
        "orphan_messages": orphans,
        "foreign_key_issues": foreign_key_issues,
        "messages_fts_count": fts_count,
        "protected_tables_before": protected_before,
        "protected_tables_after": protected_after,
        "checks": checks,
        "all_checks_passed": all(checks.values()),
    }


def execute(plan: dict[str, Any], db_path: Path, backup_dir: Path) -> dict[str, Any]:
    if not plan["safe_to_import"]:
        raise RuntimeError("Dry-run found date overlap; real write is forbidden")
    backup_path = create_complete_backup(db_path, backup_dir)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    committed = False
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        current = db_state(conn)
        if current != plan["before"]:
            raise RuntimeError("Database changed after dry-run; refusing stale import plan")
        protected_before = protected_table_fingerprints(conn)
        insert_rows(conn, "conversations", CONVERSATION_COLUMNS, plan["candidate_conversations"])
        insert_rows(conn, "messages", MESSAGE_COLUMNS, plan["candidate_messages"])
        verification = verify_state(conn, plan, protected_before)
        if not verification["all_checks_passed"]:
            raise RuntimeError("Pre-commit verification failed: " + json.dumps(verification["checks"]))
        conn.commit()
        committed = True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    with closing(ro_connect(db_path)) as verify_conn:
        verification = verify_state(verify_conn, plan, protected_before)
    if not verification["all_checks_passed"]:
        raise RuntimeError("Post-commit verification failed")
    return {
        "backup_path": str(backup_path),
        "transaction_committed": committed,
        "verification": verification,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("zip_path", type=Path)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUPS)
    parser.add_argument("--execute", action="store_true", help="Back up and import after a fresh dry-run")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.zip_path.is_file():
        raise FileNotFoundError(args.zip_path)
    if not args.db.is_file():
        raise FileNotFoundError(args.db)
    plan = prepare_plan(args.zip_path, args.db)
    print("DRY_RUN")
    print(json.dumps(public_plan(plan), ensure_ascii=False, indent=2))
    if not plan["safe_to_import"]:
        print("STOPPED: date overlap detected; no database write was attempted.", file=sys.stderr)
        return 2
    if args.execute:
        result = execute(plan, args.db, args.backup_dir)
        print("IMPORT_RESULT")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
