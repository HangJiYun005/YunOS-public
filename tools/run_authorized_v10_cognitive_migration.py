"""Fixed CLI for the one authorized v10 cognitive production migration.

No subcommand accepts a database/control path, SQL text, force option,
downgrade, destructive rollback, or policy override.  The execution module
rebuilds all production identities from code and reads only fixed canonical
control records.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memory.cognitive_production_migration import (  # noqa: E402
    COGNITIVE_DDL_DIGEST,
    COGNITIVE_PRODUCTION_MIGRATION_ID,
    COGNITIVE_PRODUCTION_ROLLBACK_POLICY,
    CognitiveProductionMigrationError,
    cognitive_production_recovery_policy,
    execute_cognitive_production_migration,
    inspect_fixed_production_cognitive_migration_plan,
    reconcile_cognitive_production_migration,
)
from memory.operational_state_models import canonical_json  # noqa: E402


TARGET_GENERATION = 10001
PRODUCTION_DATABASE_RELATIVE_PATH = (
    "data/operational/ayunos_operational_state.db"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="YunOS v10 fixed cognitive production migration boundary"
    )
    subcommands = parser.add_subparsers(dest="action", required=True)
    subcommands.add_parser(
        "inspect-plan",
        help="read-only fixed production plan inspection",
    )
    subcommands.add_parser(
        "recovery-policy",
        help="print the fixed verified-backup-only recovery policy",
    )

    execute = subcommands.add_parser(
        "execute",
        help="execute the fixed one-shot production migration",
    )
    execute.add_argument("--execute", action="store_true", required=True)
    execute.add_argument(
        "--confirm-target-generation",
        type=int,
        choices=(TARGET_GENERATION,),
        required=True,
    )
    execute.add_argument(
        "--confirm-migration-id",
        choices=(COGNITIVE_PRODUCTION_MIGRATION_ID,),
        required=True,
    )
    execute.add_argument(
        "--confirm-ddl-digest",
        choices=(COGNITIVE_DDL_DIGEST,),
        required=True,
    )
    execute.add_argument(
        "--confirm-production-database",
        choices=(PRODUCTION_DATABASE_RELATIVE_PATH,),
        required=True,
    )
    execute.add_argument(
        "--confirm-rollback-policy",
        choices=(COGNITIVE_PRODUCTION_ROLLBACK_POLICY,),
        required=True,
    )

    reconcile = subcommands.add_parser(
        "reconcile",
        help="classify a claimed execution without replaying migration SQL",
    )
    reconcile.add_argument("--reconcile", action="store_true", required=True)
    reconcile.add_argument(
        "--confirm-migration-id",
        choices=(COGNITIVE_PRODUCTION_MIGRATION_ID,),
        required=True,
    )
    reconcile.add_argument(
        "--confirm-rollback-policy",
        choices=(COGNITIVE_PRODUCTION_ROLLBACK_POLICY,),
        required=True,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.action == "inspect-plan":
            result = inspect_fixed_production_cognitive_migration_plan()
        elif arguments.action == "recovery-policy":
            result = cognitive_production_recovery_policy()
        elif arguments.action == "execute":
            result = execute_cognitive_production_migration()
        elif arguments.action == "reconcile":
            result = reconcile_cognitive_production_migration()
        else:  # argparse owns the closed action set.
            raise CognitiveProductionMigrationError(
                "production_migration_action_invalid"
            )
    except CognitiveProductionMigrationError as exc:
        print(
            canonical_json(
                {
                    "ok": False,
                    "reason_code": exc.reason_code,
                    "recovery_required": exc.recovery_required,
                }
            ),
            file=sys.stderr,
        )
        return 2
    print(canonical_json({"ok": True, "result": result}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
