from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_DB = PROJECT_ROOT / "data" / ("ayunos_" + "chatgpt.db")
SIDECAR_DB = PROJECT_ROOT / "data" / "semantic" / "ayunos_semantic.db"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.lifecycle_orchestrator import build_lifecycle_execution_plan, execute_lifecycle_plan


def _safe_plan() -> dict:
    if not SIDECAR_DB.exists():
        return {"status": "sidecar_not_initialized", "executable": False, "action_count": 0,
                "status_counts": {}, "action_counts": {}, "items": []}
    plan = build_lifecycle_execution_plan(SOURCE_DB, SIDECAR_DB, "whale")
    return {"status": "no_action" if plan["action_count"] == 0 else "ready",
            "plan_id": plan["plan_id"], "action_count": plan["action_count"],
            "status_counts": plan["status_counts"], "action_counts": _action_counts(plan),
            "provider_identity": plan["provider_identity"], "executable": False,
            "confirmation_prompt": "Confirm this plan ID explicitly before production sidecar execution.", "items": plan["items"]}


def _action_counts(plan: dict) -> dict:
    counts: dict[str, int] = {}
    for item in plan["items"]:
        action = item["action"]
        counts[action] = counts.get(action, 0) + 1
    return counts


def _write_audit(result: dict) -> None:
    try:
        root = PROJECT_ROOT / "logs" / "lifecycle"
        root.mkdir(parents=True, exist_ok=True)
        safe = {k: result.get(k) for k in ("run_id", "plan_id", "plan_schema_version", "target_kind", "target_scope", "started_at", "completed_at", "status", "executed", "transaction_committed", "transaction_rolled_back", "backup_created", "backup_name", "backup_sha256", "planned_count", "inserted_count", "duplicate_count", "failed_count", "before_status_counts", "after_status_counts", "readonly_source")}
        (root / f"lifecycle_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json").write_text(json.dumps(safe, ensure_ascii=True, indent=2), encoding="utf-8")
    except OSError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="One-shot memory lifecycle maintenance")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-plan-id")
    parser.add_argument("--real-readonly", action="store_true")
    parser.add_argument("--allow-production-sidecar", action="store_true")
    parser.add_argument("--confirm-production-sidecar-write", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if not (args.execute or args.prepare):
        args.check = True
    if args.check or args.prepare:
        result = _safe_plan()
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    if not (args.real_readonly and args.allow_production_sidecar and args.confirm_production_sidecar_write and args.confirm_plan_id):
        print(json.dumps({"status": "refused", "executable": False, "reason": "all execution confirmations are required"}))
        return 2
    if not SIDECAR_DB.exists():
        print(json.dumps({"status": "sidecar_not_initialized", "executed": False}))
        return 2
    result = execute_lifecycle_plan(SOURCE_DB, SIDECAR_DB, "whale", execute=True, confirm_plan_id=args.confirm_plan_id, allow_production_sidecar=True, confirm_production_sidecar_write=True)
    if result.get("status") in {"committed", "rolled_back", "maintenance_busy", "failed"}:
        _write_audit(result)
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if result.get("status") in {"committed", "no_action"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
