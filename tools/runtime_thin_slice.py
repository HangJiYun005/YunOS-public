from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.runtime_thin_slice import (
    RuntimeThinSliceError,
    RuntimeThinSliceService,
    open_production_runtime_execution,
    runtime_sidecar_status,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="YunOS v8.0-B/C fixed-source single-turn runtime CLI."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    preview = commands.add_parser("preview", help="Build a source-backed request artifact without writing or sending.")
    preview.add_argument("--question", required=True)
    preview.add_argument("--budget-profile", choices=("Small", "Medium", "Large"), default="Medium")

    execute = commands.add_parser("execute", help="Execute one fixed DeepSeek non-streaming request.")
    execute.add_argument("--question", required=True)
    execute.add_argument("--request-id", required=True)
    execute.add_argument("--mutation-id", required=True)
    execute.add_argument("--budget-profile", choices=("Small", "Medium", "Large"), default="Medium")
    _add_execution_confirmations(execute)

    status = commands.add_parser("status", help="Read one persisted run without writing.")
    status.add_argument("--model-run-id", required=True)

    recover = commands.add_parser("recover", help="Fail-close an interrupted running run without re-sending.")
    recover.add_argument("--model-run-id", required=True)
    _add_execution_confirmations(recover)

    commands.add_parser("sidecar-status", help="Inspect fixed production runtime sidecar availability.")
    return parser.parse_args(argv)


def _add_execution_confirmations(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--allow-external-model-call", action="store_true")
    parser.add_argument("--confirm-deepseek-send", action="store_true")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    service = RuntimeThinSliceService()
    try:
        if args.command == "preview":
            result = service.preview(args.question, budget_profile=args.budget_profile)
        elif args.command == "execute":
            authority = open_production_runtime_execution(
                execute=args.execute,
                allow_external_model_call=args.allow_external_model_call,
                confirm_deepseek_send=args.confirm_deepseek_send,
            )
            result = service.execute(
                args.question,
                request_id=args.request_id,
                mutation_id=args.mutation_id,
                budget_profile=args.budget_profile,
                authority=authority,
            )
        elif args.command == "recover":
            authority = open_production_runtime_execution(
                execute=args.execute,
                allow_external_model_call=args.allow_external_model_call,
                confirm_deepseek_send=args.confirm_deepseek_send,
            )
            result = service.recover_running(args.model_run_id, authority=authority)
        elif args.command == "status":
            result = service.get_run(args.model_run_id)
        else:
            result = runtime_sidecar_status()
    except RuntimeThinSliceError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
