from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.runtime_enablement import (
    PRODUCTION_RUNTIME_SIDECAR,
    initialize_runtime_enablement_sidecar,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Initialize the YunOS v8.0-D1 v4-flash runtime sidecar.")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--test-sidecar", type=Path)
    parser.add_argument("--allow-production-runtime-sidecar", action="store_true")
    parser.add_argument("--confirm-production-runtime-sidecar-init", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    target = args.test_sidecar or PRODUCTION_RUNTIME_SIDECAR
    result = initialize_runtime_enablement_sidecar(
        target,
        execute=args.execute,
        test_mode=args.test_sidecar is not None,
        allow_production_runtime_sidecar=args.allow_production_runtime_sidecar,
        confirm_production_runtime_sidecar_init=args.confirm_production_runtime_sidecar_init,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] in {"would_initialize", "initialized", "already_initialized"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
