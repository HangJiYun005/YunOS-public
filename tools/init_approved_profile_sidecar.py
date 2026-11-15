from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.approved_profile_control import (
    PRODUCTION_PROFILE_SIDECAR,
    initialize_approved_profile_sidecar,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Explicitly initialize the YunOS v8.0-A Lite approved-profile sidecar."
    )
    parser.add_argument("--execute", action="store_true", help="Execute; otherwise report dry-run status.")
    parser.add_argument("--test-sidecar", type=Path, help="Synthetic/test target only.")
    parser.add_argument("--allow-production-profile-sidecar", action="store_true")
    parser.add_argument("--confirm-production-profile-sidecar-init", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    target = args.test_sidecar or PRODUCTION_PROFILE_SIDECAR
    result = initialize_approved_profile_sidecar(
        target,
        execute=args.execute,
        test_mode=args.test_sidecar is not None,
        allow_production_profile_sidecar=args.allow_production_profile_sidecar,
        confirm_production_profile_sidecar_init=args.confirm_production_profile_sidecar_init,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] in {"would_initialize", "initialized", "already_initialized"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
