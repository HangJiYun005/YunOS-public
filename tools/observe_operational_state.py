from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.operational_state_acquisition import (  # noqa: E402
    DatabaseObservationAdapter,
    GitObservationAdapter,
)
from memory.operational_state_models import (  # noqa: E402
    OperationalStateError,
    canonical_json,
)


def _json_default(value):
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def _observe_git(args: argparse.Namespace) -> dict:
    return GitObservationAdapter(
        args.repository,
        repository_alias=args.repository_alias,
    ).observe(
        project_id=args.project_id,
        observed_at=args.observed_at,
    )


def _observe_database(args: argparse.Namespace) -> dict:
    return DatabaseObservationAdapter(
        args.database,
        database_alias=args.database_alias,
    ).observe(
        project_id=args.project_id,
        observed_at=args.observed_at,
        count_tables=args.count_table,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only YunOS v9 operational-state evidence observation. "
            "The command writes no source database, repository, or state store."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    git_parser = subparsers.add_parser("git")
    git_parser.add_argument("--repository", required=True)
    git_parser.add_argument("--repository-alias", required=True)
    git_parser.add_argument("--project-id", required=True)
    git_parser.add_argument("--observed-at")
    git_parser.set_defaults(handler=_observe_git)

    database_parser = subparsers.add_parser("database")
    database_parser.add_argument("--database", required=True)
    database_parser.add_argument("--database-alias", required=True)
    database_parser.add_argument("--project-id", required=True)
    database_parser.add_argument("--observed-at")
    database_parser.add_argument(
        "--count-table",
        action="append",
        help="Count one named table. Repeat as needed; omit to count all tables.",
    )
    database_parser.set_defaults(handler=_observe_database)
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    try:
        payload = args.handler(args)
    except OperationalStateError as exc:
        print(
            canonical_json({"status": "error", "reason_code": exc.reason_code}),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            payload,
            default=_json_default,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
