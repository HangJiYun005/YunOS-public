from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path: sys.path.insert(0, str(PROJECT_ROOT))

from memory.memory_sidecar import PRODUCTION_MEMORY_SIDECAR
from memory.summary_acceptance import execute_acceptance, prepare_acceptance


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare or explicitly accept one deterministic summary candidate.")
    mode = parser.add_mutually_exclusive_group(required=True); mode.add_argument("--prepare", action="store_true"); mode.add_argument("--execute", action="store_true")
    parser.add_argument("--conversation-id", required=True); parser.add_argument("--memory-space", choices=("general", "whale"))
    parser.add_argument("--confirm-candidate-id"); parser.add_argument("--confirm-plan-id")
    parser.add_argument("--real-readonly", action="store_true"); parser.add_argument("--allow-production-memory-sidecar", action="store_true"); parser.add_argument("--confirm-production-summary-write", action="store_true")
    parser.add_argument("--test-source-db", type=Path); parser.add_argument("--test-sidecar", type=Path); parser.add_argument("--test-backup-dir", type=Path)
    parser.add_argument("--show-text", action="store_true"); parser.add_argument("--json", action="store_true")
    args = parser.parse_args(); source = args.test_source_db or PROJECT_ROOT / "data" / ("ayunos_" + "chatgpt.db"); sidecar = args.test_sidecar or PRODUCTION_MEMORY_SIDECAR
    if bool(args.test_source_db) != bool(args.test_sidecar): parser.error("test source and sidecar must be supplied together")
    payload = prepare_acceptance(source, sidecar, args.conversation_id, args.memory_space) if args.prepare else execute_acceptance(source, sidecar, args.conversation_id, confirm_candidate_id=args.confirm_candidate_id, confirm_plan_id=args.confirm_plan_id, real_readonly=args.real_readonly, allow_production_memory_sidecar=args.allow_production_memory_sidecar, confirm_production_summary_write=args.confirm_production_summary_write, backup_dir=args.test_backup_dir)
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) if args.json else json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["status"] in {"prepared", "accepted", "already_accepted", "sidecar_not_initialized"} else 2


if __name__ == "__main__": raise SystemExit(main())
