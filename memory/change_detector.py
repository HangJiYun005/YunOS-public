"""Read-only comparison of conversation snapshots and saved summaries."""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from memory.memory_sidecar import PRODUCTION_MEMORY_SIDECAR
from memory.source_snapshot import connect_readonly, snapshots_from_connection, table_columns, table_exists
from memory.summary_repository import latest_accepted_summaries


SCHEMA_VERSION = 1
STATUS_ACTIONS = {
    "up_to_date": "none", "new_conversation": "none", "summary_missing": "create_summary_candidate",
    "new_messages": "refresh_summary_candidate", "source_changed": "refresh_summary_candidate",
    "coverage_unknown": "manual_review", "orphaned_summary": "manual_review",
}
HASH_COLUMNS = ("source_snapshot_hash", "source_content_hash", "source_hash", "covered_source_hash")
COUNT_COLUMNS = ("source_message_count", "message_count", "covered_message_count")
TIMESTAMP_COLUMNS = ("covered_at", "covered_timestamp", "source_covered_at")
LAST_ID_COLUMNS = ("last_message_id", "covered_last_message_id", "source_last_message_id")


def detect_changes(
    db_path: str | Path, memory_space: str | None = None, conversation_id: str | None = None,
    memory_sidecar_path: str | Path | None = None,
) -> dict[str, Any]:
    conn = connect_readonly(db_path)
    try:
        snapshots = snapshots_from_connection(conn, memory_space, conversation_id)
        summaries = _summary_records(conn, memory_space, conversation_id)
    finally:
        conn.close()

    sidecar = latest_accepted_summaries(memory_sidecar_path or PRODUCTION_MEMORY_SIDECAR, memory_space, conversation_id)
    sidecar_by_conversation = {str(item["conversation_id"]): item for item in sidecar}
    summary_by_conversation: dict[str, list[dict[str, Any]]] = {}
    for summary in summaries:
        key = summary.get("conversation_id")
        if key:
            summary_by_conversation.setdefault(str(key), []).append(summary)
    items = [
        _classify(snapshot, [sidecar_by_conversation[snapshot["conversation_id"]]], "memory_sidecar")
        if snapshot["conversation_id"] in sidecar_by_conversation
        else _classify(snapshot, summary_by_conversation.get(snapshot["conversation_id"], []), "legacy")
        for snapshot in snapshots
    ]
    known = {snapshot["conversation_id"] for snapshot in snapshots}
    for summary in sidecar:
        key = summary.get("conversation_id")
        if key and str(key) not in known:
            items.append(_item(str(key), str(summary.get("memory_space") or ""), "orphaned_summary", "memory_sidecar", summary.get("version")))
    for summary in summaries:
        key = summary.get("conversation_id")
        if key and str(key) not in known and str(key) not in sidecar_by_conversation:
            items.append(_item(str(key), str(summary.get("memory_space") or ""), "orphaned_summary", "legacy", None))
    items.sort(key=lambda item: (item["memory_space"], item["conversation_id"], item["status"]))
    statuses, actions = Counter(item["status"] for item in items), Counter(item["suggested_action"] for item in items)
    return {
        "schema_version": SCHEMA_VERSION,
        "totals": {"conversations": len(snapshots), "items": len(items)},
        "status_counts": dict(sorted(statuses.items())),
        "action_counts": dict(sorted(actions.items())),
        "items": items,
    }


def _summary_records(conn, memory_space: str | None, conversation_id: str | None) -> list[dict[str, Any]]:
    if not table_exists(conn, "memory_summaries"):
        return []
    columns = table_columns(conn, "memory_summaries")
    if "conversation_id" not in columns:
        return []
    selected = ["conversation_id"] + [name for name in ("id", "memory_space", "summary_type", "created_at", *HASH_COLUMNS, *COUNT_COLUMNS, *TIMESTAMP_COLUMNS, *LAST_ID_COLUMNS) if name in columns]
    where, params = ["conversation_id IS NOT NULL"], []
    if "summary_type" in columns:
        where.append("summary_type = 'conversation_summary'")
    if memory_space and "memory_space" in columns:
        where.append("memory_space = ?"); params.append(memory_space)
    if conversation_id:
        where.append("conversation_id = ?"); params.append(conversation_id)
    cursor = conn.execute(f"SELECT {', '.join(dict.fromkeys(selected))} FROM memory_summaries WHERE {' AND '.join(where)}", params)
    return [dict(zip([col[0] for col in cursor.description], row)) for row in cursor.fetchall()]


def _classify(snapshot: dict[str, Any], summaries: list[dict[str, Any]], summary_source: str) -> dict[str, Any]:
    if not summaries:
        return _item(snapshot["conversation_id"], snapshot["memory_space"], "new_conversation" if not snapshot["message_count"] else "summary_missing", "none", None)
    summary = max(summaries, key=lambda row: (str(row.get("created_at") or ""), str(row.get("id") or "")))
    version = summary.get("version") if summary_source == "memory_sidecar" else None
    source_hash = _first(summary, HASH_COLUMNS)
    source_count = _first(summary, COUNT_COLUMNS)
    if not source_hash or source_count is None:
        return _item(snapshot["conversation_id"], snapshot["memory_space"], "coverage_unknown", summary_source, version)
    try:
        covered_count = int(source_count)
    except (TypeError, ValueError):
        return _item(snapshot["conversation_id"], snapshot["memory_space"], "coverage_unknown", summary_source, version)
    if snapshot["message_count"] > covered_count:
        return _item(snapshot["conversation_id"], snapshot["memory_space"], "new_messages", summary_source, version)
    if snapshot["message_count"] == covered_count and source_hash != snapshot["source_content_hash"]:
        return _item(snapshot["conversation_id"], snapshot["memory_space"], "source_changed", summary_source, version)
    if snapshot["message_count"] == covered_count and source_hash == snapshot["source_content_hash"]:
        return _item(snapshot["conversation_id"], snapshot["memory_space"], "up_to_date", summary_source, version)
    return _item(snapshot["conversation_id"], snapshot["memory_space"], "source_changed", summary_source, version)


def _first(record: dict[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        if record.get(name) not in (None, ""):
            return record[name]
    return None


def _item(conversation_id: str, memory_space: str, status: str, summary_source: str, summary_version: int | None) -> dict[str, Any]:
    return {"conversation_id": conversation_id, "memory_space": memory_space, "status": status, "suggested_action": STATUS_ACTIONS[status], "summary_source": summary_source, "summary_version": summary_version}
