"""CLI for one exact, separately authorized real ChatGPT production import."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.production_real_import_entry import (  # noqa: E402
    ProductionRealImportExecutionError,
    ProductionRealImportRequest,
    execute_authorized_real_import,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--approval-package", required=True, type=Path)
    parser.add_argument("--authorization-record", required=True, type=Path)
    parser.add_argument("--import-plan-artifact", required=True, type=Path)
    parser.add_argument("--source-manifest-artifact", required=True, type=Path)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--production-db", required=True, type=Path)
    parser.add_argument("--production-asset-root", required=True, type=Path)
    parser.add_argument("--expected-package-digest", required=True)
    parser.add_argument("--expected-plan-digest", required=True)
    parser.add_argument("--expected-archive-sha256", required=True)
    parser.add_argument("--expected-database-sha256", required=True)
    parser.add_argument("--execution-root", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    request = ProductionRealImportRequest(
        approval_package=args.approval_package,
        authorization_record=args.authorization_record,
        import_plan_artifact=args.import_plan_artifact,
        source_manifest_artifact=args.source_manifest_artifact,
        archive=args.archive,
        production_db=args.production_db,
        production_asset_root=args.production_asset_root,
        expected_package_digest=args.expected_package_digest,
        expected_plan_digest=args.expected_plan_digest,
        expected_archive_sha256=args.expected_archive_sha256,
        expected_database_sha256=args.expected_database_sha256,
        execution_root=args.execution_root,
    )
    try:
        outcome = execute_authorized_real_import(request)
    except ProductionRealImportExecutionError as exc:
        print(json.dumps({
            "status": "refused_or_failed",
            "reason_code": exc.reason_code,
            "recovery_required": exc.recovery_required,
        }, sort_keys=True))
        return 2
    print(json.dumps({
        "status": outcome.status,
        "execution_id": outcome.execution_id,
        "database_after_sha256": outcome.database_after_sha256,
        "verification_digest": outcome.verification_digest,
        "recovery_required": outcome.recovery_required,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
