from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.runtime_enablement import (
    RuntimeEnablementError,
    create_production_v4_flash_runtime,
)
from memory.production_runtime_status import production_runtime_status


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Readonly YunOS v8.0-D1 v4-flash runtime surface.")
    commands = parser.add_subparsers(dest="command", required=True)
    preview = commands.add_parser("preview")
    preview.add_argument("--question", required=True)
    preview.add_argument("--budget-profile", choices=("Small", "Medium", "Large"), default="Medium")
    commands.add_parser("sidecar-status")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "preview":
            result = create_production_v4_flash_runtime().preview(
                args.question, budget_profile=args.budget_profile,
            )
        else:
            result = production_runtime_status()
    except RuntimeEnablementError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
