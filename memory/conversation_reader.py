from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any


CURSOR_VERSION = 1
MAX_PAGE_LIMIT = 20
_CURSOR_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_CURSOR_DOMAIN = b"ayunos-conversation-reader-v1\0"


class ConversationReaderError(Exception):
    pass


class ConversationReaderBadRequest(ConversationReaderError):
    pass


class ConversationReaderNotFound(ConversationReaderError):
    pass


class ConversationReaderConflict(ConversationReaderError):
    pass


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    path = Path(db_path).resolve()
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM pragma_table_info(?)", (table_name,))
    }


def _optional_column(columns: set[str], alias: str, name: str) -> str:
    return f'{alias}."{name}"' if name in columns else "NULL"


def _canonical_json(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")


def _cursor_checksum(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_CURSOR_DOMAIN + _canonical_json(payload)).hexdigest()[:24]


def _encode_cursor(
    conversation_id: str,
    direction: str,
    boundary: sqlite3.Row,
    focus: sqlite3.Row,
) -> str:
    payload = {
        "conversation_id": conversation_id,
        "direction": direction,
        "focus_message_id": focus["message_id"],
        "focus_position": int(focus["position"]),
        "focus_rowid": int(focus["message_rowid"]),
        "message_id": boundary["message_id"] or "",
        "order": [
            int(boundary["ordinal_missing"]),
            boundary["ordinal"],
            int(boundary["create_time_missing"]),
            boundary["create_time"],
            int(boundary["message_rowid"]),
        ],
        "position": int(boundary["position"]),
        "rowid": int(boundary["message_rowid"]),
        "total_messages": int(boundary["total_messages"]),
        "v": CURSOR_VERSION,
    }
    envelope = dict(payload)
    envelope["checksum"] = _cursor_checksum(payload)
    return base64.urlsafe_b64encode(_canonical_json(envelope)).decode("ascii").rstrip("=")


def _decode_cursor(raw_cursor: str) -> dict[str, Any]:
    if not raw_cursor or len(raw_cursor) > 2048 or not _CURSOR_RE.fullmatch(raw_cursor):
        raise ConversationReaderBadRequest("Invalid conversation cursor")
    try:
        padding = "=" * (-len(raw_cursor) % 4)
        decoded = base64.urlsafe_b64decode(raw_cursor + padding)
        if base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=") != raw_cursor:
            raise ValueError("non-canonical cursor")
        envelope = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConversationReaderBadRequest("Invalid conversation cursor") from exc
    expected_keys = {
        "checksum", "conversation_id", "direction", "focus_message_id",
        "focus_position", "focus_rowid", "message_id", "order", "position",
        "rowid", "total_messages", "v",
    }
    if not isinstance(envelope, dict) or set(envelope) != expected_keys:
        raise ConversationReaderBadRequest("Invalid conversation cursor")
    checksum = envelope.pop("checksum")
    if not isinstance(checksum, str) or checksum != _cursor_checksum(envelope):
        raise ConversationReaderBadRequest("Invalid conversation cursor")
    if (
        envelope.get("v") != CURSOR_VERSION
        or envelope.get("direction") not in {"before", "after"}
        or not isinstance(envelope.get("conversation_id"), str)
        or not isinstance(envelope.get("message_id"), str)
        or not isinstance(envelope.get("focus_message_id"), str)
        or isinstance(envelope.get("rowid"), bool)
        or not isinstance(envelope.get("rowid"), int)
        or envelope["rowid"] < 1
        or isinstance(envelope.get("focus_rowid"), bool)
        or not isinstance(envelope.get("focus_rowid"), int)
        or envelope["focus_rowid"] < 1
        or isinstance(envelope.get("position"), bool)
        or not isinstance(envelope.get("position"), int)
        or envelope["position"] < 1
        or isinstance(envelope.get("focus_position"), bool)
        or not isinstance(envelope.get("focus_position"), int)
        or envelope["focus_position"] < 1
        or isinstance(envelope.get("total_messages"), bool)
        or not isinstance(envelope.get("total_messages"), int)
        or envelope["total_messages"] < 1
        or not isinstance(envelope.get("order"), list)
        or len(envelope["order"]) != 5
    ):
        raise ConversationReaderBadRequest("Invalid conversation cursor")
    return envelope


def _safe_metadata(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _model_slug(metadata: dict[str, Any]) -> str:
    for key in ("model_slug", "model", "model_name"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _ordered_messages_sql(message_columns: set[str]) -> str:
    ordinal = _optional_column(message_columns, "m", "ordinal")
    create_time = _optional_column(message_columns, "m", "create_time")
    selected = [
        "m.rowid AS message_rowid",
        "m.message_id AS message_id",
        "m.conversation_id AS conversation_id",
    ]
    for name in (
        "node_id", "ordinal", "parent_id", "role", "author_name", "create_time",
        "create_time_iso", "update_time_iso", "content_type", "text", "metadata_json",
        "source_platform", "memory_space",
    ):
        selected.append(f'{_optional_column(message_columns, "m", name)} AS "{name}"')
    selected.extend([
        f"CASE WHEN {ordinal} IS NULL THEN 1 ELSE 0 END AS ordinal_missing",
        f"CASE WHEN {create_time} IS NULL THEN 1 ELSE 0 END AS create_time_missing",
        (
            "ROW_NUMBER() OVER (ORDER BY "
            f"CASE WHEN {ordinal} IS NULL THEN 1 ELSE 0 END, {ordinal}, "
            f"CASE WHEN {create_time} IS NULL THEN 1 ELSE 0 END, {create_time}, m.rowid"
            ") AS position"
        ),
        "COUNT(*) OVER () AS total_messages",
    ])
    return "SELECT\n" + ",\n".join(selected) + "\nFROM messages m WHERE m.conversation_id = ?"


def _conversation_row(
    conn: sqlite3.Connection,
    conversation_id: str,
    conversation_columns: set[str],
) -> sqlite3.Row:
    selected = ["c.conversation_id AS conversation_id"]
    for name in (
        "title", "create_time_iso", "update_time_iso", "message_count",
        "default_model_slug", "is_archived", "is_starred", "source_platform", "memory_space",
    ):
        selected.append(f'{_optional_column(conversation_columns, "c", name)} AS "{name}"')
    row = conn.execute(
        "SELECT " + ", ".join(selected) + " FROM conversations c WHERE c.conversation_id = ?",
        (conversation_id,),
    ).fetchone()
    if row is None:
        raise ConversationReaderNotFound("Conversation not found")
    return row


def _row_order(row: sqlite3.Row) -> list[Any]:
    return [
        int(row["ordinal_missing"]),
        row["ordinal"],
        int(row["create_time_missing"]),
        row["create_time"],
        int(row["message_rowid"]),
    ]


def _validate_cursor_row(
    cursor: dict[str, Any],
    direction: str,
    conversation_id: str,
    rows_by_id: dict[int, sqlite3.Row],
) -> tuple[sqlite3.Row, sqlite3.Row]:
    if cursor["direction"] != direction:
        raise ConversationReaderBadRequest("Conversation cursor direction mismatch")
    if cursor["conversation_id"] != conversation_id:
        raise ConversationReaderBadRequest("Conversation cursor belongs to another conversation")
    boundary = rows_by_id.get(cursor["rowid"])
    focus = rows_by_id.get(cursor["focus_rowid"])
    if boundary is None or focus is None:
        raise ConversationReaderConflict("Conversation cursor is stale")
    if (
        (boundary["message_id"] or "") != cursor["message_id"]
        or _row_order(boundary) != cursor["order"]
        or focus["message_id"] != cursor["focus_message_id"]
        or int(boundary["position"]) != cursor["position"]
        or int(focus["position"]) != cursor["focus_position"]
        or int(boundary["total_messages"]) != cursor["total_messages"]
    ):
        raise ConversationReaderConflict("Conversation cursor is stale")
    return boundary, focus


def _message_payload(row: sqlite3.Row, focus_rowid: int, default_model_slug: str) -> dict[str, Any]:
    metadata = _safe_metadata(row["metadata_json"])
    message_model_slug = _model_slug(metadata)
    resolved_model_slug = message_model_slug or default_model_slug
    source_platform = str(row["source_platform"] or "chatgpt").strip() or "chatgpt"
    return {
        "conversation_id": row["conversation_id"] or "",
        "message_id": row["message_id"] or "",
        "node_id": row["node_id"] or "",
        "ordinal": row["ordinal"],
        "position": int(row["position"]),
        "parent_id": row["parent_id"] or "",
        "role": row["role"] or "",
        "author_name": row["author_name"] or "",
        "create_time": row["create_time"],
        "create_time_iso": row["create_time_iso"] or "",
        "update_time_iso": row["update_time_iso"] or "",
        "content_type": row["content_type"] or "",
        "text": row["text"] or "",
        "source_platform": source_platform,
        "memory_space": row["memory_space"] or "",
        "model_slug": message_model_slug,
        "default_model_slug": default_model_slug,
        "model_trace": {
            "message_model_slug": message_model_slug,
            "conversation_default_model_slug": default_model_slug,
            "resolved_model_slug": resolved_model_slug,
        },
        "is_focus": int(row["message_rowid"]) == focus_rowid,
    }


def read_conversation_messages(
    db_path: Path,
    *,
    conversation_id: str,
    anchor_message_id: str | None = None,
    before: str | None = None,
    after: str | None = None,
    limit: int = MAX_PAGE_LIMIT,
) -> dict[str, Any]:
    conversation_id = str(conversation_id or "").strip()
    if not conversation_id:
        raise ConversationReaderBadRequest("Missing conversation id")
    modes = [anchor_message_id is not None, before is not None, after is not None]
    if sum(modes) != 1:
        raise ConversationReaderBadRequest("Exactly one conversation anchor is required")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_PAGE_LIMIT:
        raise ConversationReaderBadRequest("Invalid conversation limit")
    if anchor_message_id is not None and not str(anchor_message_id).strip():
        raise ConversationReaderBadRequest("Missing anchor message id")

    with _connect_readonly(Path(db_path)) as conn:
        conversation_columns = _table_columns(conn, "conversations")
        message_columns = _table_columns(conn, "messages")
        if "conversation_id" not in conversation_columns or not {"conversation_id", "message_id"} <= message_columns:
            raise sqlite3.DatabaseError("Conversation reader schema is unavailable")
        conversation_row = _conversation_row(conn, conversation_id, conversation_columns)
        ordered_sql = _ordered_messages_sql(message_columns)
        mode = "anchor"
        if anchor_message_id is not None:
            anchor_id = str(anchor_message_id).strip()
            anchors = conn.execute(
                f"WITH ordered AS ({ordered_sql}) "
                "SELECT * FROM ordered WHERE message_id = ? LIMIT 2",
                (conversation_id, anchor_id),
            ).fetchall()
            if not anchors:
                raise ConversationReaderNotFound("Message anchor not found")
            if len(anchors) != 1:
                raise ConversationReaderConflict("Ambiguous message anchor")
            focus = anchors[0]
            total_messages = int(focus["total_messages"])
            desired_before = limit // 2
            start = max(1, int(focus["position"]) - desired_before)
            end = min(total_messages, start + limit - 1)
            start = max(1, end - limit + 1)
            page_rows = conn.execute(
                f"WITH ordered AS ({ordered_sql}) "
                "SELECT * FROM ordered WHERE position BETWEEN ? AND ? ORDER BY position",
                (conversation_id, start, end),
            ).fetchall()
        else:
            mode = "before" if before is not None else "after"
            cursor = _decode_cursor(before if before is not None else str(after))
            if cursor["direction"] != mode:
                raise ConversationReaderBadRequest("Conversation cursor direction mismatch")
            if cursor["conversation_id"] != conversation_id:
                raise ConversationReaderBadRequest(
                    "Conversation cursor belongs to another conversation"
                )
            cursor_rows = conn.execute(
                f"WITH ordered AS ({ordered_sql}) "
                "SELECT * FROM ordered WHERE message_rowid IN (?, ?)",
                (conversation_id, cursor["rowid"], cursor["focus_rowid"]),
            ).fetchall()
            rows_by_id = {int(row["message_rowid"]): row for row in cursor_rows}
            boundary, focus = _validate_cursor_row(cursor, mode, conversation_id, rows_by_id)
            boundary_position = int(boundary["position"])
            total_messages = int(boundary["total_messages"])
            if mode == "before":
                page_rows = conn.execute(
                    f"WITH ordered AS ({ordered_sql}) "
                    "SELECT * FROM ordered WHERE position < ? "
                    "ORDER BY position DESC LIMIT ?",
                    (conversation_id, boundary_position, limit),
                ).fetchall()[::-1]
            else:
                page_rows = conn.execute(
                    f"WITH ordered AS ({ordered_sql}) "
                    "SELECT * FROM ordered WHERE position > ? ORDER BY position LIMIT ?",
                    (conversation_id, boundary_position, limit),
                ).fetchall()

        first_position = int(page_rows[0]["position"]) if page_rows else 0
        last_position = int(page_rows[-1]["position"]) if page_rows else 0
        has_before = bool(page_rows and first_position > 1)
        has_after = bool(page_rows and last_position < total_messages)
        before_cursor = (
            _encode_cursor(conversation_id, "before", page_rows[0], focus)
            if has_before else None
        )
        after_cursor = (
            _encode_cursor(conversation_id, "after", page_rows[-1], focus)
            if has_after else None
        )

        conversation = dict(conversation_row)
        conversation["title"] = conversation.get("title") or ""
        conversation["create_time_iso"] = conversation.get("create_time_iso") or ""
        conversation["update_time_iso"] = conversation.get("update_time_iso") or ""
        conversation["default_model_slug"] = conversation.get("default_model_slug") or ""
        conversation["source_platform"] = conversation.get("source_platform") or "chatgpt"
        conversation["memory_space"] = conversation.get("memory_space") or ""
        conversation["total_message_count"] = total_messages
        default_model_slug = str(conversation["default_model_slug"])
        focus_payload = {
            "message_id": focus["message_id"] or "",
            "ordinal": focus["ordinal"],
            "position": int(focus["position"]),
            "is_loaded": any(
                int(row["message_rowid"]) == int(focus["message_rowid"])
                for row in page_rows
            ),
        }
        return {
            "conversation": conversation,
            "focus": focus_payload,
            "messages": [
                _message_payload(row, int(focus["message_rowid"]), default_model_slug)
                for row in page_rows
            ],
            "cursors": {"before": before_cursor, "after": after_cursor},
            "has_before": has_before,
            "has_after": has_after,
            "limit": limit,
            "mode": mode,
        }
