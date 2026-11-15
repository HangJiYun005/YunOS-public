from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.db_path_policy import TestDatabasePathError
from memory.lifecycle_executor import execute_memory_lifecycle


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Execute planned semantic embedding lifecycle work in a test database.")
    parser.add_argument("--source-db", required=True)
    parser.add_argument("--target-db", required=True)
    parser.add_argument("--memory-space")
    parser.add_argument("--provider", default="local_sentence_transformer")
    parser.add_argument("--execute", action="store_true", help="Apply the plan. Omit for the default dry run.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = execute_memory_lifecycle(
            args.source_db, args.target_db, args.memory_space, args.provider, dry_run=not args.execute
        )
    except (TestDatabasePathError, RuntimeError, ValueError) as exc:
        print(json.dumps({"dry_run": not args.execute, "executed": False, "status": "refused", "reason": str(exc)}, ensure_ascii=True))
        return 2
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
