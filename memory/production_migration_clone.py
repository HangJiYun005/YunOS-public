"""Disposable-clone executor for production-entry protocol testing.

This module cannot target the production Main database: it delegates path
validation to the existing clone-only migration entrypoint.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from memory.ingestion_migrations import (
    FaultInjector,
    MigrationRehearsalError,
    MigrationResult,
    apply_migration_to_clone,
    validate_clone_database_path,
)
from memory.production_migration_entry import (
    ProductionMigrationExecutionError,
    append_execution_journal_event,
)


def execute_v8301_clone_protocol(
    *,
    plan,
    clone_path: str | Path,
    execution_root: str | Path,
    execution_id: str,
    fault_injector: FaultInjector | None = None,
    now: datetime | None = None,
) -> MigrationResult:
    """Exercise transaction, journal, rollback, and recovery on a clone only."""
    clone = validate_clone_database_path(clone_path)
    root = Path(execution_root).resolve(strict=True)
    if root != clone.parent.resolve(strict=True):
        raise ProductionMigrationExecutionError("clone_execution_root_mismatch")

    def event(name: str) -> None:
        try:
            append_execution_journal_event(
                root,
                execution_id=execution_id,
                event=name,
                now=now,
            )
        except ProductionMigrationExecutionError as exc:
            raise OSError("clone_journal_event_failed") from exc

    try:
        result = apply_migration_to_clone(
            plan,
            clone,
            rehearsal_id=execution_id,
            fault_injector=fault_injector,
            event_hook=event,
        )
        append_execution_journal_event(
            root,
            execution_id=execution_id,
            event="execution_completed",
            now=now,
        )
        return result
    except MigrationRehearsalError as exc:
        terminal_event = (
            "recovery_required" if exc.recovery_required else "execution_failed"
        )
        try:
            append_execution_journal_event(
                root,
                execution_id=execution_id,
                event=terminal_event,
                reason_code=exc.reason_code,
                now=now,
            )
        except ProductionMigrationExecutionError:
            pass
        raise
