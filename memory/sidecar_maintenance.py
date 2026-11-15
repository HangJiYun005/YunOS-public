from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memory.db_path_policy import validate_semantic_sidecar_write_target
from memory.lifecycle_planner import (
    ACTION_CREATE_EMBEDDING,
    ACTION_REBUILD_EMBEDDING,
    CONTROLLED_PLAN_SCHEMA_VERSION,
    PLAN_SCHEMA_VERSION,
    calculate_lifecycle_plan_id,
    get_lifecycle_provider_identity,
    plan_memory_lifecycle,
)
from memory.semantic_memory import (
    build_embedding_record,
    insert_embedding_record,
    is_busy_or_locked_error,
    is_unique_constraint_error,
)


BACKUP_ROOT = Path(__file__).resolve().parents[1] / "backups" / "semantic"
SUPPORTED_ACTIONS = {ACTION_CREATE_EMBEDDING, ACTION_REBUILD_EMBEDDING}


def execute_confirmed_sidecar_maintenance(
    source_db: str | Path,
    sidecar_db: str | Path,
    memory_space: str | None = None,
    provider_name: str = "local_sentence_transformer",
    *,
    confirmed_plan_schema_version: str | None = None,
    confirmed_plan_id: str | None = None,
    confirmed_plan_items: list[dict[str, Any]] | None = None,
    confirmed_provider_identity: dict[str, Any] | None = None,
    confirmed_target_kind: str | None = None,
    allow_production_sidecar: bool = False,
    confirm_production_sidecar_write: bool = False,
    memory_sidecar_path: str | Path | None = None,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    """Validate a complete confirmed envelope before entering the private transaction."""
    target = validate_semantic_sidecar_write_target(sidecar_db)
    target_kind = "production_semantic_sidecar"
    current = plan_memory_lifecycle(
        source_db, target, memory_space, provider_name,
        memory_sidecar_path, conversation_id,
    )
    current_items = sorted(
        [
            _confirmed_item(item)
            for item in current["items"]
            if item["recommended_action"] in SUPPORTED_ACTIONS
        ],
        key=_item_key,
    )
    audit_args = {
        "plan_id": confirmed_plan_id,
        "plan_schema_version": confirmed_plan_schema_version or PLAN_SCHEMA_VERSION,
        "target_kind": target_kind,
        "planned_count": len(current_items),
        "before_status_counts": current["status_counts"],
        "after_status_counts": current["status_counts"],
    }
    expected_schema_version = CONTROLLED_PLAN_SCHEMA_VERSION if memory_sidecar_path is not None else PLAN_SCHEMA_VERSION
    if not (allow_production_sidecar and confirm_production_sidecar_write):
        return build_lifecycle_audit(status="refused", **audit_args)
    if (
        confirmed_plan_schema_version != expected_schema_version
        or not confirmed_plan_id
        or confirmed_plan_items is None
        or confirmed_provider_identity is None
        or confirmed_target_kind != target_kind
        or not _valid_items(confirmed_plan_items)
    ):
        return build_lifecycle_audit(status="refused", **audit_args)

    expected_items = sorted(confirmed_plan_items, key=_item_key)
    current_identity = get_lifecycle_provider_identity(provider_name)
    current_plan_id = calculate_lifecycle_plan_id(
        target_kind, current_identity, current_items, expected_schema_version,
    )
    if (
        current_plan_id != confirmed_plan_id
        or current_items != expected_items
        or current_identity != confirmed_provider_identity
    ):
        return build_lifecycle_audit(status="stale_plan", **audit_args)

    records: list[dict] = []
    confirmed_by_id = {item["summary_id"]: item for item in expected_items}
    for item in expected_items:
        summary = _load_summary(source_db, item, memory_sidecar_path)
        if summary is None:
            return build_lifecycle_audit(status="stale_plan", **audit_args)
        content_hash = hashlib.sha256(summary["summary_text"].encode("utf-8")).hexdigest()
        if content_hash != confirmed_by_id[item["summary_id"]]["content_hash"]:
            return build_lifecycle_audit(status="stale_plan", **audit_args)
        records.append(
            build_embedding_record(
                summary["source_table"],
                item["summary_id"],
                summary["summary_text"],
                memory_space=summary["memory_space"],
                provider_name=provider_name,
                metadata=_summary_metadata(summary),
            )
        )

    result = _maintain_sidecar_transaction(
        records,
        target,
        confirmed_plan_id,
        plan_schema_version=confirmed_plan_schema_version,
        target_kind=target_kind,
        before_status_counts=current["status_counts"],
    )
    after = plan_memory_lifecycle(
        source_db, target, memory_space, provider_name,
        memory_sidecar_path, conversation_id,
    )
    result["after_status_counts"] = after["status_counts"]
    return result


def build_lifecycle_audit(
    *,
    status: str,
    plan_id: str | None,
    plan_schema_version: str,
    target_kind: str,
    planned_count: int,
    before_status_counts: dict[str, int] | None = None,
    after_status_counts: dict[str, int] | None = None,
    run_id: str | None = None,
    started_at: str | None = None,
    completed_at: str | None = None,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "plan_id": plan_id,
        "plan_schema_version": plan_schema_version,
        "target_kind": target_kind,
        "target_scope": target_kind,
        "started_at": started_at,
        "completed_at": completed_at,
        "status": status,
        "executed": status == "committed",
        "transaction_committed": status == "committed",
        "transaction_rolled_back": status == "rolled_back",
        "backup_created": False,
        "backup_name": None,
        "backup_sha256": None,
        "planned_count": planned_count,
        "inserted_count": 0,
        "duplicate_count": 0,
        "failed_count": int(status in {"rolled_back", "failed"}),
        "before_status_counts": before_status_counts or {},
        "after_status_counts": after_status_counts or before_status_counts or {},
        "readonly_source": True,
    }


def _maintain_sidecar_transaction(
    records: list[dict],
    db_path: str | Path,
    plan_id: str,
    *,
    plan_schema_version: str = PLAN_SCHEMA_VERSION,
    target_kind: str = "production_semantic_sidecar",
    before_status_counts: dict[str, int] | None = None,
) -> dict[str, Any]:
    target = validate_semantic_sidecar_write_target(db_path)
    started = datetime.now(timezone.utc)
    started_at = started.isoformat()
    run_id = hashlib.sha256(f"{started_at}:{plan_id}".encode()).hexdigest()[:20]
    base = build_lifecycle_audit(
        status="pending",
        plan_id=plan_id,
        plan_schema_version=plan_schema_version,
        target_kind=target_kind,
        planned_count=len(records),
        before_status_counts=before_status_counts,
        run_id=run_id,
        started_at=started_at,
    )
    if not records:
        return _complete(base, "no_action")

    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    timestamp = started.strftime("%Y%m%dT%H%M%S%fZ")
    backup_name = f"ayunos_semantic_before_{timestamp}_{plan_id[:12]}_{run_id}.db"
    backup = BACKUP_ROOT / backup_name
    try:
        source_conn = sqlite3.connect(target)
        backup_conn = sqlite3.connect(backup)
        try:
            source_conn.backup(backup_conn)
        finally:
            backup_conn.close()
            source_conn.close()
        base.update({
            "backup_created": True,
            "backup_name": backup_name,
            "backup_sha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
        })
    except (OSError, sqlite3.Error):
        return _complete(base, "failed")

    conn = sqlite3.connect(target, timeout=0)
    try:
        conn.execute("BEGIN IMMEDIATE")
        for record in records:
            try:
                insert_embedding_record(conn, record)
                base["inserted_count"] += 1
            except sqlite3.IntegrityError as exc:
                if not is_unique_constraint_error(exc):
                    raise
                base["duplicate_count"] += 1
        conn.commit()
        return _complete(base, "committed")
    except sqlite3.OperationalError as exc:
        conn.rollback()
        base["inserted_count"] = 0
        if is_busy_or_locked_error(exc):
            backup.unlink(missing_ok=True)
            base.update({"backup_created": False, "backup_name": None, "backup_sha256": None})
            return _complete(base, "maintenance_busy")
        return _complete(base, "rolled_back")
    except Exception:
        conn.rollback()
        base["inserted_count"] = 0
        return _complete(base, "rolled_back")
    finally:
        conn.close()


def _complete(result: dict[str, Any], status: str) -> dict[str, Any]:
    result.update({
        "status": status,
        "executed": status == "committed",
        "transaction_committed": status == "committed",
        "transaction_rolled_back": status == "rolled_back",
        "failed_count": int(status in {"rolled_back", "failed"}),
        "completed_at": datetime.now(timezone.utc).isoformat(),
    })
    return result


def _load_summary(
    source_db: str | Path,
    item: dict[str, Any],
    memory_sidecar_path: str | Path | None,
) -> dict[str, Any] | None:
    summary_id = str(item["summary_id"])
    memory_space = item["memory_space"]
    source_table = str(item.get("source_table") or "memory_summaries")
    if source_table == "summary_versions" and memory_sidecar_path is None:
        return None
    path = Path(memory_sidecar_path if source_table == "summary_versions" else source_db).expanduser().resolve()
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        if source_table == "summary_versions":
            row = conn.execute(
                """SELECT summary_id AS id, memory_space, 'conversation_summary' AS summary_type,
                          conversation_id, summary_text, summary_text_hash, version AS summary_version,
                          candidate_id, acceptance_plan_id, source_snapshot_hash
                   FROM summary_versions WHERE summary_id = ? AND memory_space IS ?""",
                (summary_id, memory_space),
            ).fetchone()
            if row is None:
                return None
            return {**dict(row), "summary_source": "memory_sidecar", "source_table": "summary_versions",
                    "semantic_target_kind": "summary_embedding"}
        row = conn.execute(
            """SELECT id, memory_space, summary_type, conversation_id, summary_text
               FROM memory_summaries WHERE id = ? AND memory_space IS ?""",
            (summary_id, memory_space),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result.update({"summary_source": "legacy", "source_table": "memory_summaries",
                       "summary_version": None, "candidate_id": None, "acceptance_plan_id": None,
                       "source_snapshot_hash": None,
                       "summary_text_hash": hashlib.sha256(str(result["summary_text"]).encode("utf-8")).hexdigest(),
                       "semantic_target_kind": "summary_embedding"})
        return result
    finally:
        conn.close()


def _summary_metadata(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary_source": summary["summary_source"],
        "summary_id": str(summary["id"]),
        "conversation_id": summary["conversation_id"],
        "summary_version": summary.get("summary_version"),
        "summary_text_hash": summary["summary_text_hash"],
        "memory_space": summary["memory_space"],
        "candidate_id": summary.get("candidate_id"),
        "acceptance_plan_id": summary.get("acceptance_plan_id"),
        "source_snapshot_hash": summary.get("source_snapshot_hash"),
        "semantic_target_kind": summary["semantic_target_kind"],
        "summary_type": summary["summary_type"],
    }


def _confirmed_item(item: dict[str, Any]) -> dict[str, Any]:
    result = {
        "summary_id": item["summary_id"],
        "memory_space": item["memory_space"],
        "action": item["recommended_action"],
        "content_hash": item["content_hash"],
    }
    if item.get("summary_source"):
        for key in (
            "summary_source", "source_table", "conversation_id", "summary_version",
            "summary_text_hash", "candidate_id", "acceptance_plan_id",
            "source_snapshot_hash", "semantic_target_kind",
        ):
            result[key] = item.get(key)
    return result


def _item_key(item: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(item["summary_id"]),
        str(item["memory_space"]),
        str(item["action"]),
        str(item["content_hash"]),
    )


def _valid_items(items: list[dict[str, Any]]) -> bool:
    legacy = {"summary_id", "memory_space", "action", "content_hash"}
    controlled = legacy | {
        "summary_source", "source_table", "conversation_id", "summary_version",
        "summary_text_hash", "candidate_id", "acceptance_plan_id",
        "source_snapshot_hash", "semantic_target_kind",
    }
    allowed = {frozenset(legacy), frozenset(controlled)}
    return isinstance(items, list) and all(
        isinstance(item, dict) and frozenset(item) in allowed for item in items
    )
