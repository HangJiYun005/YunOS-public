from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REAL_DB_PATH = PROJECT_ROOT / "data" / ("ayunos_" + "chatgpt.db")
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.lifecycle_planner import plan_memory_lifecycle


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plan semantic-memory lifecycle work without executing it.")
    parser.add_argument("--source-db", required=True)
    parser.add_argument("--semantic-db", required=True)
    parser.add_argument("--memory-space")
    parser.add_argument("--provider", default="local_sentence_transformer")
    parser.add_argument("--real-readonly", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source = Path(args.source_db).expanduser().resolve()
    if source == REAL_DB_PATH.resolve() and not args.real_readonly:
        print(json.dumps({"readonly": True, "executed": False, "status": "refused", "reason": "real source requires --real-readonly"}, ensure_ascii=True))
        return 2
    try:
        plan = plan_memory_lifecycle(source, args.semantic_db, args.memory_space, args.provider)
    except (RuntimeError, ValueError) as exc:
        print(json.dumps({"readonly": True, "executed": False, "status": "blocked", "reason": str(exc)}, ensure_ascii=True))
        return 2
    print(json.dumps(plan, ensure_ascii=False if args.json else True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
