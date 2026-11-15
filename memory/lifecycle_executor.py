from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from memory import db_path_policy
from memory.db_path_policy import validate_semantic_sidecar_write_target, validate_test_db_write_target
from memory.lifecycle_planner import (
    ACTION_CREATE_EMBEDDING,
    ACTION_REBUILD_EMBEDDING,
    PLAN_SCHEMA_VERSION,
    calculate_lifecycle_plan_id,
    get_lifecycle_provider_identity,
    plan_memory_lifecycle,
)
from memory.semantic_memory import build_embedding_record, is_busy_or_locked_error, save_embedding_record
from memory.sidecar_maintenance import build_lifecycle_audit, execute_confirmed_sidecar_maintenance


SUPPORTED_ACTIONS = {ACTION_CREATE_EMBEDDING, ACTION_REBUILD_EMBEDDING}


def execute_memory_lifecycle(
    source_db: str | Path,
    target_db: str | Path,
    memory_space: str | None = None,
    provider_name: str = "local_sentence_transformer",
    dry_run: bool = True,
    summary_ids: set[str] | None = None,
    allow_production_sidecar: bool = False,
    confirmed_plan_id: str | None = None,
    confirmed_plan_schema_version: str | None = None,
    confirmed_plan_items: list[dict[str, Any]] | None = None,
    confirmed_provider_identity: dict[str, Any] | None = None,
    confirmed_target_kind: str | None = None,
    confirm_production_sidecar_write: bool = False,
    memory_sidecar_path: str | Path | None = None,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    """Execute only planner-approved embedding work against a controlled test DB.

    The source is opened read-only and only ``memory_summaries`` is queried.  A
    non-dry run is rejected unless the target passes the shared test-DB policy.
    """
    target = Path(target_db).expanduser()
    is_production_target = (
        target.resolve(strict=False)
        == db_path_policy.DEFAULT_SEMANTIC_SIDECAR_PATH.resolve(strict=False)
    )
    if not dry_run:
        target = (
            validate_semantic_sidecar_write_target(target)
            if is_production_target else validate_test_db_write_target(target)
        )

    plan = plan_memory_lifecycle(
        source_db, target, memory_space, provider_name,
        memory_sidecar_path, conversation_id,
    )
    operations = [
        item for item in plan["items"]
        if item["recommended_action"] in SUPPORTED_ACTIONS
        and (summary_ids is None or item["summary_id"] in summary_ids)
    ]
    result = {
        "status": "dry_run" if dry_run else "pending",
        "dry_run": dry_run,
        "executed": not dry_run,
        "provider_identity": plan["provider_identity"],
        "plan": _safe_plan(plan),
        "result_counts": {"planned": len(operations), "inserted": 0, "already_current": 0, "skipped": 0},
        "operations": [],
        "audit": {"target_scope": "controlled_test_memory_embeddings", "raw_messages_read": False,
                  "real_database_write": False, "network_called": False,
                  "plan_fingerprint": _plan_fingerprint(operations)},
        "warnings": list(plan["warnings"]),
    }
    if plan.get("overall_status") or plan["status_counts"].get("blocked"):
        result["executed"] = False
        result["warnings"].append("planner is blocked; no lifecycle operation was attempted")
        return result

    if is_production_target and not dry_run:
        maintenance = execute_confirmed_sidecar_maintenance(
            source_db,
            target,
            memory_space,
            provider_name,
            confirmed_plan_schema_version=confirmed_plan_schema_version,
            confirmed_plan_id=confirmed_plan_id,
            confirmed_plan_items=confirmed_plan_items,
            confirmed_provider_identity=confirmed_provider_identity,
            confirmed_target_kind=confirmed_target_kind,
            allow_production_sidecar=allow_production_sidecar,
            confirm_production_sidecar_write=confirm_production_sidecar_write,
            memory_sidecar_path=memory_sidecar_path,
            conversation_id=conversation_id,
        )
        result["maintenance"] = maintenance
        result["result_counts"]["inserted"] = maintenance["inserted_count"]
        result["operations"] = [{**_operation_base(item), "status": "inserted" if maintenance["status"] == "committed" else maintenance["status"]} for item in operations]
        result.update({
            "status": maintenance["status"],
            "executed": maintenance["executed"],
            "plan_id": confirmed_plan_id,
            "plan_schema_version": maintenance["plan_schema_version"],
            "target_kind": maintenance["target_kind"],
            "audit": dict(maintenance),
        })
        return result

    for item in operations:
        operation = _operation_base(item)
        if dry_run:
            operation["status"] = "planned"
            result["operations"].append(operation)
            continue

        summary = _load_summary(source_db, item, memory_sidecar_path)
        if summary is None:
            operation["status"] = "skipped"
            operation["reason"] = "summary is no longer available"
            result["result_counts"]["skipped"] += 1
            result["operations"].append(operation)
            continue

        record = build_embedding_record(
            summary["source_table"], item["summary_id"], summary["summary_text"],
            memory_space=summary["memory_space"], provider_name=provider_name,
            metadata=_summary_metadata(summary),
        )
        try:
            write_result = save_embedding_record(
                record, target, dry_run=False, allow_production_sidecar=allow_production_sidecar
            )
        except sqlite3.OperationalError as exc:
            operation["status"] = "maintenance_busy" if is_busy_or_locked_error(exc) else "failed"
            operation["audit"]["write_performed"] = False
            result["result_counts"]["skipped"] += 1
            result["operations"].append(operation)
            result.update({"status": operation["status"], "executed": False})
            return result
        except (OSError, RuntimeError, ValueError, sqlite3.DatabaseError):
            operation["status"] = "failed"
            operation["audit"]["write_performed"] = False
            result["result_counts"]["skipped"] += 1
            result["operations"].append(operation)
            result.update({"status": "failed", "executed": False})
            return result
        if write_result["written"]:
            operation["status"] = "inserted"
            operation["audit"]["write_performed"] = True
            result["result_counts"]["inserted"] += 1
        else:
            operation["status"] = "already_current"
            operation["audit"]["write_performed"] = False
            result["result_counts"]["already_current"] += 1
        result["operations"].append(operation)
    if not dry_run:
        result.update({
            "status": "committed" if operations else "no_action",
            "executed": bool(operations),
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
                       "source_snapshot_hash": None, "summary_text_hash": hashlib.sha256(str(result["summary_text"]).encode("utf-8")).hexdigest(),
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


def _safe_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary_count": plan["summary_count"], "status_counts": plan["status_counts"],
        "action_counts": plan["action_counts"],
        "items": [_operation_base(item) for item in plan["items"] if item["recommended_action"] in SUPPORTED_ACTIONS],
    }


def _operation_base(item: dict[str, Any]) -> dict[str, Any]:
    operation = {key: item[key] for key in ("summary_id", "memory_space", "content_hash", "status", "recommended_action")}
    for key in (
        "summary_source", "source_table", "conversation_id", "summary_version",
        "summary_text_hash", "candidate_id", "acceptance_plan_id",
        "source_snapshot_hash", "semantic_target_kind",
    ):
        if key in item:
            operation[key] = item[key]
    operation["audit"] = {
        "source_table": "memory_summaries",
        "target_scope": "controlled_test_memory_embeddings",
        "raw_messages_read": False,
        "network_called": False,
        "write_performed": False,
    }
    return operation


def _plan_fingerprint(items: list[dict[str, Any]]) -> str:
    stable = [
        {key: item[key] for key in ("summary_id", "memory_space", "content_hash", "recommended_action")}
        for item in items
    ]
    encoded = json.dumps(stable, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _confirmed_item_key(item: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(item["summary_id"]),
        str(item["memory_space"]),
        str(item["action"]),
        str(item["content_hash"]),
    )


def _valid_confirmed_items(items: list[dict[str, Any]]) -> bool:
    legacy = {"summary_id", "memory_space", "action", "content_hash"}
    controlled = legacy | {
        "summary_source", "source_table", "conversation_id", "summary_version",
        "summary_text_hash", "candidate_id", "acceptance_plan_id",
        "source_snapshot_hash", "semantic_target_kind",
    }
    allowed = {frozenset(legacy), frozenset(controlled)}
    return isinstance(items, list) and all(
        isinstance(item, dict) and frozenset(item) in allowed
        for item in items
    )
