from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.memory_sidecar import PRODUCTION_MEMORY_SIDECAR, initialize_memory_sidecar


def main() -> int:
    parser = argparse.ArgumentParser(description="Initialize the versioned memory-summary sidecar schema only.")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--real-readonly", action="store_true")
    parser.add_argument("--allow-production-memory-sidecar", action="store_true")
    parser.add_argument("--confirm-production-memory-sidecar-init", action="store_true")
    parser.add_argument("--test-db", type=Path, help="Test-only target; production path is fixed.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    payload = initialize_memory_sidecar(args.test_db or PRODUCTION_MEMORY_SIDECAR, execute=args.execute, real_readonly=args.real_readonly, allow_production_memory_sidecar=args.allow_production_memory_sidecar, confirm_production_memory_sidecar_init=args.confirm_production_memory_sidecar_init)
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) if args.json else payload)
    return 0 if payload["status"] in {"initialized", "already_initialized", "would_initialize"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
