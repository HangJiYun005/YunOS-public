from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REAL_DB_PATH = PROJECT_ROOT / "data" / ("ayunos_" + "chatgpt.db")
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.lifecycle_orchestrator import execute_lifecycle_plan


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build and confirm a controlled memory lifecycle plan.")
    parser.add_argument("--source-db", required=True)
    parser.add_argument("--semantic-db", required=True)
    parser.add_argument("--memory-space")
    parser.add_argument("--provider", default="local_sentence_transformer")
    parser.add_argument("--max-actions", type=int, default=20)
    parser.add_argument("--real-readonly", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-plan-id")
    parser.add_argument("--allow-production-sidecar", action="store_true")
    parser.add_argument("--confirm-production-sidecar-write", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source = Path(args.source_db).expanduser().resolve()
    if source == REAL_DB_PATH.resolve() and not args.real_readonly:
        print(json.dumps({"status": "refused", "readonly": True, "executed": False, "reason": "real source requires --real-readonly"}))
        return 2
    result = execute_lifecycle_plan(
        source, args.semantic_db, args.memory_space, args.provider, args.max_actions,
        execute=args.execute, confirm_plan_id=args.confirm_plan_id,
        allow_production_sidecar=args.allow_production_sidecar,
        confirm_production_sidecar_write=args.confirm_production_sidecar_write,
    )
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if result["status"] in {"dry_run", "no_action", "committed"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
