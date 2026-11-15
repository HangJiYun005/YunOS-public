"""Command-line entry for one exact, separately authorized v8301 execution."""
from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.production_migration_entry import (
    ProductionMigrationExecutionError,
    ProductionMigrationRequest,
    apply_authorized_production_migration,
    validate_target_version,
)


def _exact_target(value: str) -> int:
    try:
        parsed = int(value, 10)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(
            "target_version_must_be_exact_8301"
        ) from None
    try:
        return validate_target_version(parsed)
    except ProductionMigrationExecutionError as exc:
        raise argparse.ArgumentTypeError(exc.reason_code) from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Execute exact v8301 only with a v2 approval package and a "
            "separate one-time HumanAuthorizationRecord."
        ),
        allow_abbrev=False,
    )
    parser.add_argument("--target-version", required=True, type=_exact_target)
    parser.add_argument("--approval-package", required=True, type=Path)
    parser.add_argument("--authorization-record", required=True, type=Path)
    parser.add_argument("--production-db", required=True, type=Path)
    parser.add_argument("--expected-db-sha256", required=True)
    parser.add_argument("--expected-current-schema", required=True)
    parser.add_argument("--expected-target-schema", required=True)
    parser.add_argument("--expected-migration-digest", required=True)
    parser.add_argument("--expected-plan-digest", required=True)
    parser.add_argument("--execution-root", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    request = ProductionMigrationRequest(
        target_version=args.target_version,
        approval_package=args.approval_package,
        authorization_record=args.authorization_record,
        production_db=args.production_db,
        expected_db_sha256=args.expected_db_sha256,
        expected_current_schema=args.expected_current_schema,
        expected_target_schema=args.expected_target_schema,
        expected_migration_digest=args.expected_migration_digest,
        expected_plan_digest=args.expected_plan_digest,
        execution_root=args.execution_root,
    )
    try:
        result = apply_authorized_production_migration(request)
    except ProductionMigrationExecutionError as exc:
        print(json.dumps({
            "status": "recovery_required" if exc.recovery_required else "refused",
            "reason_code": exc.reason_code,
            "recovery_required": exc.recovery_required,
        }, sort_keys=True))
        return 3 if exc.recovery_required else 2
    payload = result.to_dict() if hasattr(result, "to_dict") else (
        asdict(result) if is_dataclass(result) else {"status": "unknown"}
    )
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
