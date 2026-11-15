"""Recoverable two-confirmation orchestration for one controlled memory refresh."""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memory.change_detector import detect_changes
from memory.lifecycle_orchestrator import build_lifecycle_execution_plan, execute_lifecycle_plan
from memory.lifecycle_planner import UP_TO_DATE, get_lifecycle_provider_identity, plan_memory_lifecycle
from memory.memory_sidecar import sidecar_initialized
from memory.semantic_memory import SemanticRetrievalError, semantic_summary_search_preview
from memory.summary_acceptance import execute_acceptance, prepare_acceptance
from memory.summary_repository import (
    accepted_summary_by_candidate,
    latest_accepted_summary,
)


PHASES = {
    "status",
    "prepare-summary",
    "execute-summary",
    "prepare-semantic",
    "execute-semantic",
    "verify",
}


def run_memory_refresh(
    source_db: str | Path,
    memory_sidecar: str | Path,
    semantic_db: str | Path,
    conversation_id: str,
    *,
    memory_space: str | None = None,
    provider_name: str = "local_sentence_transformer",
    phase: str = "status",
    confirm_candidate_id: str | None = None,
    confirm_acceptance_plan_id: str | None = None,
    confirm_semantic_plan_id: str | None = None,
    real_readonly: bool = False,
    allow_production_memory_sidecar: bool = False,
    confirm_production_summary_write: bool = False,
    allow_production_semantic_sidecar: bool = False,
    confirm_production_semantic_write: bool = False,
    backup_dir: str | Path | None = None,
    show_candidate_text: bool = False,
) -> dict[str, Any]:
    """Run exactly one explicit phase; the default phase is read-only status."""
    if phase not in PHASES:
        raise ValueError("Unsupported memory refresh phase")
    started_at = _utc()
    base = _base_audit(conversation_id, provider_name, phase, started_at)

    if phase == "status":
        result = _status(
            source_db, memory_sidecar, semantic_db, conversation_id,
            memory_space, provider_name, base,
        )
    elif phase == "prepare-summary":
        result = _prepare_summary(
            source_db, memory_sidecar, conversation_id, memory_space,
            base, show_candidate_text,
        )
    elif phase == "execute-summary":
        result = _execute_summary(
            source_db, memory_sidecar, conversation_id, memory_space, base,
            confirm_candidate_id, confirm_acceptance_plan_id,
            real_readonly, allow_production_memory_sidecar,
            confirm_production_summary_write, backup_dir,
        )
    elif phase == "prepare-semantic":
        result = _prepare_semantic(
            source_db, memory_sidecar, semantic_db, conversation_id,
            memory_space, provider_name, base,
        )
    elif phase == "execute-semantic":
        result = _execute_semantic(
            source_db, memory_sidecar, semantic_db, conversation_id,
            memory_space, provider_name, base, confirm_semantic_plan_id,
            allow_production_semantic_sidecar, confirm_production_semantic_write,
        )
    else:
        result = _verify_completion(
            source_db, memory_sidecar, semantic_db, conversation_id,
            memory_space, provider_name, base,
        )

    result["completed_at"] = _utc()
    return result


def _status(
    source_db: str | Path,
    memory_sidecar: str | Path,
    semantic_db: str | Path,
    conversation_id: str,
    memory_space: str | None,
    provider_name: str,
    base: dict[str, Any],
) -> dict[str, Any]:
    return _verify_completion(
        source_db, memory_sidecar, semantic_db, conversation_id,
        memory_space, provider_name, base,
    )


def _prepare_summary(
    source_db: str | Path,
    memory_sidecar: str | Path,
    conversation_id: str,
    memory_space: str | None,
    base: dict[str, Any],
    show_candidate_text: bool,
) -> dict[str, Any]:
    prepared = prepare_acceptance(
        source_db, memory_sidecar, conversation_id, memory_space,
        include_candidate_text=show_candidate_text,
    )
    plan = prepared.get("acceptance_plan") or {}
    candidate = prepared.get("candidate") or {}
    base.update({
        "candidate_id": candidate.get("candidate_id"),
        "acceptance_plan_id": plan.get("acceptance_plan_id"),
    })
    if show_candidate_text and candidate.get("candidate_text") is not None:
        base["candidate_preview"] = candidate["candidate_text"]
    status = str(prepared.get("status") or "manual_review")
    if status == "prepared" and prepared.get("executable"):
        return _finish(base, "summary_acceptance_required", "execute_summary_acceptance")
    if prepared.get("target_status") == "sidecar_not_initialized":
        return _finish(base, "sidecar_not_initialized", "initialize_memory_sidecar")
    if status == "no_eligible_candidate":
        accepted = latest_accepted_summary(memory_sidecar, conversation_id, memory_space)
        if accepted:
            _bind_accepted(base, accepted)
            return _finish(base, "summary_accepted_index_pending", "prepare_semantic_maintenance")
    return _finish(base, status, "manual_review")


def _execute_summary(
    source_db: str | Path,
    memory_sidecar: str | Path,
    conversation_id: str,
    memory_space: str | None,
    base: dict[str, Any],
    candidate_id: str | None,
    acceptance_plan_id: str | None,
    real_readonly: bool,
    allow_production: bool,
    confirm_production: bool,
    backup_dir: str | Path | None,
) -> dict[str, Any]:
    base.update({"candidate_id": candidate_id, "acceptance_plan_id": acceptance_plan_id})
    executed = execute_acceptance(
        source_db,
        memory_sidecar,
        conversation_id,
        confirm_candidate_id=candidate_id,
        confirm_plan_id=acceptance_plan_id,
        real_readonly=real_readonly,
        allow_production_memory_sidecar=allow_production,
        confirm_production_summary_write=confirm_production,
        backup_dir=backup_dir,
    )
    _bind_backup(base, executed)
    status = str(executed.get("status") or "failed")
    if status not in {"accepted", "already_accepted"}:
        return _finish(base, status, _summary_next_action(status))
    accepted = accepted_summary_by_candidate(memory_sidecar, str(candidate_id or ""))
    if accepted is None:
        return _finish(base, "manual_review", "inspect_memory_sidecar")
    latest = latest_accepted_summary(memory_sidecar, conversation_id, accepted["memory_space"])
    if latest is None or latest["summary_id"] != accepted["summary_id"]:
        return _finish(base, "semantic_plan_stale", "prepare_summary_acceptance")
    _bind_accepted(base, accepted)
    return _finish(base, "summary_accepted_index_pending", "prepare_semantic_maintenance")


def _prepare_semantic(
    source_db: str | Path,
    memory_sidecar: str | Path,
    semantic_db: str | Path,
    conversation_id: str,
    memory_space: str | None,
    provider_name: str,
    base: dict[str, Any],
) -> dict[str, Any]:
    if not sidecar_initialized(memory_sidecar):
        return _finish(base, "sidecar_not_initialized", "initialize_memory_sidecar")
    accepted = latest_accepted_summary(memory_sidecar, conversation_id, memory_space)
    if accepted is None:
        return _finish(base, "summary_acceptance_required", "prepare_summary_acceptance")
    _bind_accepted(base, accepted)
    plan = build_lifecycle_execution_plan(
        source_db, semantic_db, accepted["memory_space"], provider_name,
        max_actions=1, memory_sidecar_path=memory_sidecar,
        conversation_id=conversation_id,
    )
    base["semantic_plan_id"] = plan["plan_id"]
    if plan["warnings"]:
        return _finish(base, "manual_review", "inspect_semantic_sidecar")
    if plan["action_count"] == 0:
        return _verify_completion(
            source_db, memory_sidecar, semantic_db, conversation_id,
            accepted["memory_space"], provider_name, base,
        )
    item = plan["items"][0]
    if not _plan_item_matches_accepted(item, accepted):
        return _finish(base, "semantic_plan_stale", "prepare_semantic_maintenance")
    return _finish(base, "semantic_plan_ready", "execute_semantic_maintenance")


def _execute_semantic(
    source_db: str | Path,
    memory_sidecar: str | Path,
    semantic_db: str | Path,
    conversation_id: str,
    memory_space: str | None,
    provider_name: str,
    base: dict[str, Any],
    semantic_plan_id: str | None,
    allow_production: bool,
    confirm_production: bool,
) -> dict[str, Any]:
    base["semantic_plan_id"] = semantic_plan_id
    if not semantic_plan_id:
        return _finish(base, "refused", "prepare_semantic_maintenance")
    accepted = latest_accepted_summary(memory_sidecar, conversation_id, memory_space)
    if accepted is None:
        return _finish(base, "summary_acceptance_required", "prepare_summary_acceptance")
    _bind_accepted(base, accepted)
    executed = execute_lifecycle_plan(
        source_db, semantic_db, accepted["memory_space"], provider_name,
        max_actions=1, execute=True, confirm_plan_id=semantic_plan_id,
        allow_production_sidecar=allow_production,
        confirm_production_sidecar_write=confirm_production,
        memory_sidecar_path=memory_sidecar,
        conversation_id=conversation_id,
    )
    audit = executed.get("audit") or executed.get("maintenance") or executed
    _bind_backup(base, audit)
    status = str(executed.get("status") or "failed")
    if status not in {"committed", "no_action"}:
        return _finish(base, status, _semantic_next_action(status))
    verified = _verify_completion(
        source_db, memory_sidecar, semantic_db, conversation_id,
        accepted["memory_space"], provider_name, base,
    )
    verified["semantic_plan_id"] = semantic_plan_id
    return verified


def _verify_completion(
    source_db: str | Path,
    memory_sidecar: str | Path,
    semantic_db: str | Path,
    conversation_id: str,
    memory_space: str | None,
    provider_name: str,
    base: dict[str, Any],
) -> dict[str, Any]:
    checks = {
        "accepted_summary_exists": False,
        "change_detector_up_to_date": False,
        "semantic_lifecycle_up_to_date": False,
        "semantic_binding_matches": False,
        "semantic_retrieval_hit": False,
        "readonly_source": True,
    }
    base["verification"] = checks
    if not sidecar_initialized(memory_sidecar):
        return _finish(base, "sidecar_not_initialized", "initialize_memory_sidecar")
    accepted = latest_accepted_summary(memory_sidecar, conversation_id, memory_space)
    if accepted is None:
        return _finish(base, "summary_acceptance_required", "prepare_summary_acceptance")
    _bind_accepted(base, accepted)
    checks["accepted_summary_exists"] = True
    change = _change_item(source_db, memory_sidecar, conversation_id, accepted["memory_space"])
    checks["change_detector_up_to_date"] = bool(change and change.get("status") == "up_to_date")
    if not checks["change_detector_up_to_date"]:
        return _finish(base, "summary_acceptance_required", "prepare_summary_acceptance")
    lifecycle = plan_memory_lifecycle(
        source_db, semantic_db, accepted["memory_space"], provider_name,
        memory_sidecar, conversation_id,
    )
    item = _current_lifecycle_item(lifecycle, accepted)
    checks["semantic_lifecycle_up_to_date"] = bool(item and item.get("status") == UP_TO_DATE)
    if not checks["semantic_lifecycle_up_to_date"]:
        if lifecycle.get("overall_status") or lifecycle["status_counts"].get("blocked"):
            return _finish(base, "manual_review", "inspect_semantic_sidecar")
        return _finish(base, "summary_accepted_index_pending", "prepare_semantic_maintenance")
    try:
        checks["semantic_binding_matches"] = _semantic_binding_matches(
            semantic_db, accepted, provider_name,
        )
    except (OSError, RuntimeError, ValueError):
        return _finish(base, "manual_review", "inspect_embedding_provider")
    if not checks["semantic_binding_matches"]:
        return _finish(base, "manual_review", "repair_semantic_metadata")
    try:
        preview = semantic_summary_search_preview(
            str(accepted["summary_text"]), semantic_db,
            memory_space=accepted["memory_space"], top_k=20,
            provider_name=provider_name,
        )
    except (SemanticRetrievalError, OSError, RuntimeError, ValueError):
        return _finish(base, "manual_review", "inspect_semantic_retrieval")
    checks["semantic_retrieval_hit"] = any(
        row.get("source_table") == "summary_versions"
        and row.get("source_id") == accepted["summary_id"]
        for row in preview.get("results", [])
    )
    if not checks["semantic_retrieval_hit"]:
        return _finish(base, "manual_review", "inspect_semantic_retrieval")
    base["semantic_committed"] = True
    return _finish(base, "completed", "none")


def _semantic_binding_matches(
    semantic_db: str | Path,
    accepted: dict[str, Any],
    provider_name: str,
) -> bool:
    identity = get_lifecycle_provider_identity(provider_name)
    path = Path(semantic_db).expanduser().resolve()
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT * FROM memory_embeddings
                   WHERE source_table = 'summary_versions' AND source_id = ?
                     AND memory_space = ? AND content_hash = ?""",
                (accepted["summary_id"], accepted["memory_space"], accepted["summary_text_hash"]),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return False
    for row in rows:
        record = dict(row)
        if not _provider_record_matches(record, identity):
            continue
        try:
            metadata = json.loads(record.get("metadata_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        expected = {
            "summary_source": "memory_sidecar",
            "summary_id": accepted["summary_id"],
            "conversation_id": accepted["conversation_id"],
            "summary_version": accepted["version"],
            "summary_text_hash": accepted["summary_text_hash"],
            "memory_space": accepted["memory_space"],
            "candidate_id": accepted["candidate_id"],
            "acceptance_plan_id": accepted["acceptance_plan_id"],
            "source_snapshot_hash": accepted["source_snapshot_hash"],
            "semantic_target_kind": "summary_embedding",
        }
        if all(metadata.get(key) == value for key, value in expected.items()):
            return True
    return False


def _provider_record_matches(record: dict[str, Any], identity: dict[str, Any]) -> bool:
    return (
        record.get("embedding_provider") == identity["provider"]
        and record.get("embedding_model") == identity["model"]
        and record.get("model_revision") == identity["revision"]
        and record.get("model_fingerprint") == identity["fingerprint"]
        and record.get("embedding_protocol") == identity["protocol"]
        and int(record.get("embedding_dim") or 0) == int(identity["dim"])
        and int(record.get("embedding_normalized") or 0) == int(bool(identity["normalized"]))
    )


def _change_item(
    source_db: str | Path,
    memory_sidecar: str | Path,
    conversation_id: str,
    memory_space: str | None,
) -> dict[str, Any] | None:
    detected = detect_changes(
        source_db, memory_space=memory_space, conversation_id=conversation_id,
        memory_sidecar_path=memory_sidecar,
    )
    return next((row for row in detected.get("items", []) if row.get("conversation_id") == conversation_id), None)


def _current_lifecycle_item(plan: dict[str, Any], accepted: dict[str, Any]) -> dict[str, Any] | None:
    return next(
        (
            item for item in plan.get("items", [])
            if item.get("summary_source") == "memory_sidecar"
            and item.get("summary_id") == accepted["summary_id"]
            and item.get("summary_version") == accepted["version"]
            and item.get("summary_text_hash") == accepted["summary_text_hash"]
        ),
        None,
    )


def _plan_item_matches_accepted(item: dict[str, Any], accepted: dict[str, Any]) -> bool:
    return (
        item.get("summary_source") == "memory_sidecar"
        and item.get("source_table") == "summary_versions"
        and item.get("summary_id") == accepted["summary_id"]
        and item.get("conversation_id") == accepted["conversation_id"]
        and item.get("summary_version") == accepted["version"]
        and item.get("summary_text_hash") == accepted["summary_text_hash"]
        and item.get("content_hash") == accepted["summary_text_hash"]
        and item.get("memory_space") == accepted["memory_space"]
        and item.get("candidate_id") == accepted["candidate_id"]
        and item.get("acceptance_plan_id") == accepted["acceptance_plan_id"]
        and item.get("source_snapshot_hash") == accepted["source_snapshot_hash"]
        and item.get("semantic_target_kind") == "summary_embedding"
    )


def _base_audit(
    conversation_id: str,
    provider_name: str,
    phase: str,
    started_at: str,
) -> dict[str, Any]:
    try:
        identity = get_lifecycle_provider_identity(provider_name)
        safe_identity = {
            "provider": identity["provider"],
            "model": identity["model"],
            "revision": identity["revision"],
            "fingerprint": str(identity["fingerprint"])[:12],
            "protocol": identity["protocol"],
            "dim": identity["dim"],
            "normalized": identity["normalized"],
        }
    except (OSError, RuntimeError, ValueError):
        safe_identity = {"provider": provider_name, "status": "unavailable"}
    return {
        "workflow_run_id": uuid.uuid4().hex,
        "conversation_id": conversation_id,
        "candidate_id": None,
        "acceptance_plan_id": None,
        "accepted_summary_id": None,
        "accepted_summary_version": None,
        "accepted_summary_text_hash": None,
        "semantic_plan_id": None,
        "provider_identity": safe_identity,
        "current_phase": phase,
        "status": "pending",
        "next_action": None,
        "summary_committed": False,
        "semantic_committed": False,
        "backup": {"created": False, "name": None, "sha256": None},
        "started_at": started_at,
        "completed_at": None,
        "readonly_source": True,
    }


def _bind_accepted(base: dict[str, Any], accepted: dict[str, Any]) -> None:
    base.update({
        "candidate_id": accepted["candidate_id"],
        "acceptance_plan_id": accepted["acceptance_plan_id"],
        "accepted_summary_id": accepted["summary_id"],
        "accepted_summary_version": accepted["version"],
        "accepted_summary_text_hash": accepted["summary_text_hash"],
        "summary_committed": True,
    })


def _bind_backup(base: dict[str, Any], result: dict[str, Any]) -> None:
    base["backup"] = {
        "created": bool(result.get("backup_created")),
        "name": result.get("backup_name"),
        "sha256": result.get("backup_sha256"),
    }


def _finish(base: dict[str, Any], status: str, next_action: str) -> dict[str, Any]:
    base.update({"status": status, "next_action": next_action})
    return base


def _summary_next_action(status: str) -> str:
    if status in {"stale_plan", "source_busy"}:
        return "prepare_summary_acceptance"
    if status == "sidecar_not_initialized":
        return "initialize_memory_sidecar"
    return "manual_review"


def _semantic_next_action(status: str) -> str:
    if status == "stale_plan":
        return "prepare_semantic_maintenance"
    if status in {"maintenance_busy", "failed", "rolled_back"}:
        return "retry_semantic_maintenance"
    if status == "refused":
        return "prepare_semantic_maintenance"
    return "manual_review"


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
