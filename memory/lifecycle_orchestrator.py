from __future__ import annotations

from pathlib import Path
from typing import Any

from memory.db_path_policy import (
    DEFAULT_SEMANTIC_SIDECAR_PATH,
    SemanticSidecarPathError,
    TestDatabasePathError,
    validate_semantic_sidecar_write_target,
    validate_test_db_write_target,
)
from memory.lifecycle_executor import execute_memory_lifecycle
from memory.lifecycle_planner import (
    ACTION_CREATE_EMBEDDING,
    ACTION_REBUILD_EMBEDDING,
    CONTROLLED_PLAN_SCHEMA_VERSION,
    PLAN_SCHEMA_VERSION,
    calculate_lifecycle_plan_id,
    get_lifecycle_provider_identity,
    plan_memory_lifecycle,
)


SUPPORTED_ACTIONS = {ACTION_CREATE_EMBEDDING, ACTION_REBUILD_EMBEDDING}


def build_lifecycle_execution_plan(
    source_db: str | Path,
    semantic_db: str | Path,
    memory_space: str | None = None,
    provider_name: str = "local_sentence_transformer",
    max_actions: int = 20,
    memory_sidecar_path: str | Path | None = None,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    """Create a deterministic, summary-only execution plan without writing."""
    limit = max(1, min(int(max_actions), 100))
    planned = plan_memory_lifecycle(
        source_db,
        semantic_db,
        memory_space,
        provider_name,
        memory_sidecar_path,
        conversation_id,
    )
    items = [_safe_item(item) for item in planned["items"] if item["recommended_action"] in SUPPORTED_ACTIONS]
    items.sort(key=lambda item: (item["summary_id"], item["memory_space"], item["action"], item["content_hash"]))
    items = items[:limit]
    target_kind = _target_kind(semantic_db)
    provider_identity = get_lifecycle_provider_identity(provider_name)
    schema_version = CONTROLLED_PLAN_SCHEMA_VERSION if memory_sidecar_path is not None else PLAN_SCHEMA_VERSION
    return {
        "plan_schema_version": schema_version,
        "readonly": True,
        "executed": False,
        "target_kind": target_kind,
        "provider_identity": provider_identity,
        "plan_id": calculate_lifecycle_plan_id(target_kind, provider_identity, items, schema_version),
        "action_count": len(items),
        "items": items,
        "status_counts": planned["status_counts"],
        "warnings": list(planned["warnings"]),
    }


def execute_lifecycle_plan(
    source_db: str | Path,
    semantic_db: str | Path,
    memory_space: str | None = None,
    provider_name: str = "local_sentence_transformer",
    max_actions: int = 20,
    execute: bool = False,
    confirm_plan_id: str | None = None,
    allow_production_sidecar: bool = False,
    confirm_production_sidecar_write: bool = False,
    memory_sidecar_path: str | Path | None = None,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    """Require confirmation of a freshly rebuilt plan before test-DB execution."""
    try:
        target, is_sidecar = _validate_target(semantic_db)
    except (TestDatabasePathError, SemanticSidecarPathError) as exc:
        return _refused("refused", str(exc))

    before = build_lifecycle_execution_plan(
        source_db, target, memory_space, provider_name, max_actions,
        memory_sidecar_path, conversation_id,
    )
    result: dict[str, Any] = {
        "status": "dry_run" if not execute else "refused",
        "readonly": not execute,
        "executed": False,
        "plan": before,
        "before_status_counts": before["status_counts"],
        "after_status_counts": before["status_counts"],
        "items": [],
        **_execution_audit(before, "dry_run" if not execute else "refused"),
    }
    if not execute:
        return result
    if is_sidecar and (not allow_production_sidecar or not confirm_production_sidecar_write):
        result["reason"] = "production sidecar execution requires both production confirmations"
        return result
    if not confirm_plan_id:
        result["reason"] = "confirm_plan_id is required"
        return result

    latest = build_lifecycle_execution_plan(
        source_db, target, memory_space, provider_name, max_actions,
        memory_sidecar_path, conversation_id,
    )
    result["plan"] = latest
    result["before_status_counts"] = latest["status_counts"]
    if confirm_plan_id != latest["plan_id"]:
        result.update(_execution_audit(latest, "stale_plan"))
        result["reason"] = "confirm_plan_id does not match the latest plan"
        return result

    executor_result = execute_memory_lifecycle(
        source_db, target, memory_space, provider_name, dry_run=False,
        allow_production_sidecar=is_sidecar,
        confirm_production_sidecar_write=confirm_production_sidecar_write,
        confirmed_plan_schema_version=latest["plan_schema_version"],
        confirmed_plan_id=latest["plan_id"],
        confirmed_plan_items=latest["items"],
        confirmed_provider_identity=latest["provider_identity"],
        confirmed_target_kind=latest["target_kind"],
        memory_sidecar_path=memory_sidecar_path,
        conversation_id=conversation_id,
    )
    after = plan_memory_lifecycle(
        source_db, target, memory_space, provider_name,
        memory_sidecar_path, conversation_id,
    )
    audit = dict(executor_result["audit"])
    audit.update({
        "before_status_counts": latest["status_counts"],
        "after_status_counts": after["status_counts"],
    })
    result.update({
        **audit,
        "status": executor_result["status"],
        "readonly": False,
        "executed": executor_result["executed"],
        "items": executor_result["operations"],
        "after_status_counts": after["status_counts"],
        "audit": audit,
        "maintenance": executor_result.get("maintenance"),
    })
    return result


def _safe_item(item: dict[str, Any]) -> dict[str, Any]:
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


def _refused(status: str, reason: str) -> dict[str, Any]:
    return {"status": status, "readonly": True, "executed": False, "reason": reason, "items": []}


def _validate_target(semantic_db: str | Path) -> tuple[Path, bool]:
    candidate = Path(semantic_db).expanduser()
    if candidate.resolve(strict=False) == DEFAULT_SEMANTIC_SIDECAR_PATH.resolve(strict=False):
        return validate_semantic_sidecar_write_target(candidate), True
    return validate_test_db_write_target(candidate), False


def _target_kind(semantic_db: str | Path) -> str:
    candidate = Path(semantic_db).expanduser()
    return (
        "production_semantic_sidecar"
        if candidate.resolve(strict=False) == DEFAULT_SEMANTIC_SIDECAR_PATH.resolve(strict=False)
        else "test_semantic_db"
    )


def _execution_audit(plan: dict[str, Any], status: str) -> dict[str, Any]:
    return {
        "run_id": None,
        "plan_id": plan["plan_id"],
        "plan_schema_version": plan["plan_schema_version"],
        "target_kind": plan["target_kind"],
        "target_scope": plan["target_kind"],
        "started_at": None,
        "completed_at": None,
        "status": status,
        "executed": False,
        "transaction_committed": False,
        "transaction_rolled_back": False,
        "backup_created": False,
        "backup_name": None,
        "backup_sha256": None,
        "planned_count": plan["action_count"],
        "inserted_count": 0,
        "duplicate_count": 0,
        "failed_count": 0,
        "readonly_source": True,
    }
