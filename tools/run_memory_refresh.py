from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.db_path_policy import (
    DEFAULT_MEMORY_SIDECAR_PATH,
    DEFAULT_REAL_DB_PATH,
    DEFAULT_SEMANTIC_SIDECAR_PATH,
)
from memory.refresh_workflow import PHASES, run_memory_refresh


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one explicit phase of the controlled memory refresh pipeline."
    )
    parser.add_argument("--phase", choices=sorted(PHASES), default="status")
    parser.add_argument("--source-db", type=Path, default=DEFAULT_REAL_DB_PATH)
    parser.add_argument("--memory-sidecar", type=Path, default=DEFAULT_MEMORY_SIDECAR_PATH)
    parser.add_argument("--semantic-db", type=Path, default=DEFAULT_SEMANTIC_SIDECAR_PATH)
    parser.add_argument("--conversation-id", required=True)
    parser.add_argument("--memory-space")
    parser.add_argument("--provider", default="local_sentence_transformer")
    parser.add_argument("--confirm-candidate-id")
    parser.add_argument("--confirm-acceptance-plan-id")
    parser.add_argument("--confirm-semantic-plan-id")
    parser.add_argument("--real-readonly", action="store_true")
    parser.add_argument("--allow-production-memory-sidecar", action="store_true")
    parser.add_argument("--confirm-production-summary-write", action="store_true")
    parser.add_argument("--allow-production-semantic-sidecar", action="store_true")
    parser.add_argument("--confirm-production-semantic-write", action="store_true")
    parser.add_argument("--test-backup-dir", type=Path)
    parser.add_argument(
        "--show-candidate-text",
        action="store_true",
        help="Explicitly include candidate text in this one preview response only.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = run_memory_refresh(
            args.source_db,
            args.memory_sidecar,
            args.semantic_db,
            args.conversation_id,
            memory_space=args.memory_space,
            provider_name=args.provider,
            phase=args.phase,
            confirm_candidate_id=args.confirm_candidate_id,
            confirm_acceptance_plan_id=args.confirm_acceptance_plan_id,
            confirm_semantic_plan_id=args.confirm_semantic_plan_id,
            real_readonly=args.real_readonly,
            allow_production_memory_sidecar=args.allow_production_memory_sidecar,
            confirm_production_summary_write=args.confirm_production_summary_write,
            allow_production_semantic_sidecar=args.allow_production_semantic_sidecar,
            confirm_production_semantic_write=args.confirm_production_semantic_write,
            backup_dir=args.test_backup_dir,
            show_candidate_text=args.show_candidate_text,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        result = {
            "workflow_run_id": uuid.uuid4().hex,
            "conversation_id": args.conversation_id,
            "candidate_id": args.confirm_candidate_id,
            "acceptance_plan_id": args.confirm_acceptance_plan_id,
            "accepted_summary_id": None,
            "accepted_summary_version": None,
            "accepted_summary_text_hash": None,
            "semantic_plan_id": args.confirm_semantic_plan_id,
            "provider_identity": {"provider": args.provider, "status": "unavailable"},
            "status": "failed",
            "current_phase": args.phase,
            "next_action": "manual_review",
            "summary_committed": False,
            "semantic_committed": False,
            "backup": {"created": False, "name": None, "sha256": None},
            "started_at": now,
            "completed_at": now,
            "readonly_source": True,
            "error_type": type(exc).__name__,
        }
    print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True))
    return 0 if result.get("status") in {
        "completed",
        "summary_acceptance_required",
        "summary_accepted_index_pending",
        "semantic_plan_ready",
        "sidecar_not_initialized",
    } else 2


if __name__ == "__main__":
    raise SystemExit(main())
