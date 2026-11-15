"""Versioned, append-only storage for accepted memory summaries."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

from memory.db_path_policy import (
    ControlledSummaryPathError,
    DEFAULT_MEMORY_SIDECAR_PATH,
    validate_memory_sidecar_target,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_MEMORY_SIDECAR = DEFAULT_MEMORY_SIDECAR_PATH
PRODUCTION_MEMORY_BACKUPS = PROJECT_ROOT / "backups" / "memory"

SUMMARY_VERSIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS summary_versions (
    summary_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    memory_space TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    candidate_id TEXT NOT NULL UNIQUE,
    candidate_schema_version INTEGER NOT NULL,
    source_snapshot_hash TEXT NOT NULL,
    source_message_count INTEGER NOT NULL CHECK(source_message_count >= 0),
    source_last_message_at TEXT,
    source_latest_message_id TEXT,
    generator_identity TEXT NOT NULL,
    generator_config_hash TEXT NOT NULL,
    summary_text TEXT NOT NULL,
    summary_text_hash TEXT NOT NULL,
    reason TEXT NOT NULL CHECK(reason IN ('summary_missing', 'coverage_unknown', 'new_messages', 'source_changed')),
    base_summary_id TEXT,
    base_summary_text_hash TEXT,
    acceptance_plan_id TEXT NOT NULL,
    acceptance_run_id TEXT NOT NULL,
    accepted_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(conversation_id, version)
);
CREATE INDEX IF NOT EXISTS idx_summary_versions_current
ON summary_versions(conversation_id, memory_space, version DESC);
"""


class MemorySidecarError(ValueError):
    """Raised when a sidecar target violates the fixed-path policy."""


def is_production_memory_sidecar(path: str | Path) -> bool:
    target = Path(os.path.abspath(Path(path).expanduser()))
    return target == PRODUCTION_MEMORY_SIDECAR


def sidecar_initialized(path: str | Path) -> bool:
    target = Path(path).expanduser().resolve()
    if not target.exists():
        return False
    conn = sqlite3.connect(f"file:{target.as_posix()}?mode=ro", uri=True)
    try:
        return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'summary_versions'").fetchone())
    finally:
        conn.close()


def initialize_memory_sidecar(
    path: str | Path = PRODUCTION_MEMORY_SIDECAR,
    *,
    execute: bool = False,
    real_readonly: bool = False,
    allow_production_memory_sidecar: bool = False,
    confirm_production_memory_sidecar_init: bool = False,
) -> dict[str, Any]:
    """Initialize schema only; production creation requires the full explicit gate."""
    production_hint = is_production_memory_sidecar(path)
    result = {"target_kind": "production_memory_sidecar" if production_hint else "test_memory_sidecar", "executed": False}
    try:
        target, production = validate_memory_sidecar_target(
            path,
            production_memory_path=PRODUCTION_MEMORY_SIDECAR,
        )
    except ControlledSummaryPathError as exc:
        return {**result, "status": "refused", "error": str(exc)}
    result = {"target_kind": "production_memory_sidecar" if production else "test_memory_sidecar", "executed": False}
    if not execute:
        return {**result, "status": "would_initialize" if not sidecar_initialized(target) else "already_initialized"}
    if production and not (real_readonly and allow_production_memory_sidecar and confirm_production_memory_sidecar_init):
        return {**result, "status": "refused", "error": "production memory sidecar initialization requires all confirmation flags"}
    if sidecar_initialized(target):
        return {**result, "status": "already_initialized"}
    if target.exists() and target.stat().st_size:
        return {**result, "status": "refused", "error": "existing sidecar is not initialized; refusing to overwrite it"}
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    try:
        validate_memory_sidecar_target(
            target,
            production_memory_path=PRODUCTION_MEMORY_SIDECAR,
        )
        conn.executescript(SUMMARY_VERSIONS_SCHEMA)
        conn.commit()
    except ControlledSummaryPathError as exc:
        conn.rollback()
        return {**result, "status": "refused", "error": str(exc)}
    finally:
        conn.close()
    return {**result, "status": "initialized", "executed": True}
