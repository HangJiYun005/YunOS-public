from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.deepseek_v4_adapter import DeepSeekV4ProviderError
from memory.runtime_enablement import (
    RuntimeEnablementError,
    deepseek_readiness_dry_run,
    open_production_deepseek_readiness,
    run_production_deepseek_readiness,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Explicit Gate-E DeepSeek readiness using only GET /models and GET /user/balance."
    )
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--allow-official-readiness-gets", action="store_true")
    parser.add_argument("--confirm-no-chat-completion", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.execute:
        print(json.dumps(deepseek_readiness_dry_run(), ensure_ascii=False, sort_keys=True))
        return 0
    try:
        authority = open_production_deepseek_readiness(
            execute=args.execute,
            allow_official_readiness_gets=args.allow_official_readiness_gets,
            confirm_no_chat_completion=args.confirm_no_chat_completion,
        )
        result = run_production_deepseek_readiness(authority=authority)
    except (RuntimeEnablementError, DeepSeekV4ProviderError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
