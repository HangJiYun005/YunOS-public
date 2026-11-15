"""Readonly YunOS v8.0-F status projection for the fixed D1 production Runtime."""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
from typing import Any

from memory.runtime_protocol_contracts import (
    EXPECTED_IDENTITIES,
    FIXED_RUNTIME_PROFILE,
    RuntimeProtocolContractError,
    runtime_profile_identity,
    validate_runtime_ledger,
)


STATUS_PROTOCOL_VERSION = "v8.0-f-production-runtime-status-1"
PRODUCTION_RUNTIME_SIDECAR = (
    Path(__file__).resolve().parents[1] / "data" / "runtime" / "ayunos_runtime.db"
)

EXPECTED_RUNTIME_PROFILE = FIXED_RUNTIME_PROFILE

_EXPECTED_COLUMNS = {
    "runtime_schema": {
        "schema_version", "runtime_profile_json", "runtime_profile_identity", "initialized_at",
    },
    "runtime_conversations": {"conversation_id", "created_at"},
    "runtime_request_artifacts": {
        "artifact_identity", "artifact_version", "runtime_profile_version", "renderer_version",
        "delivery_identity", "delivery_json", "approved_profile_projection_identity",
        "provider", "model", "base_url", "stream", "thinking_type", "wire_body_sha256",
        "artifact_json", "created_at",
    },
    "runtime_model_runs": {
        "model_run_id", "request_id", "mutation_id", "request_fingerprint",
        "conversation_id", "artifact_identity", "created_at",
    },
    "runtime_messages": {
        "message_id", "conversation_id", "model_run_id", "role", "content", "created_at",
    },
    "runtime_errors": {
        "error_id", "model_run_id", "category", "code", "retryable", "http_status",
        "sent_to_model", "external_model_calls", "created_at",
    },
    "runtime_run_events": {
        "sequence", "event_id", "model_run_id", "event_type", "from_status", "to_status",
        "assistant_message_id", "error_id", "usage_json", "provider_response_id",
        "finish_reason", "response_content_sha256", "occurred_at",
    },
    "runtime_receipts": {"model_run_id", "receipt_identity", "receipt_json"},
}

_COUNT_TABLES = {
    "conversations": "runtime_conversations",
    "request_artifacts": "runtime_request_artifacts",
    "model_runs": "runtime_model_runs",
    "messages": "runtime_messages",
    "events": "runtime_run_events",
    "errors": "runtime_errors",
    "receipts": "runtime_receipts",
}


class _StatusProblem(RuntimeError):
    def __init__(self, status: str, reason_code: str) -> None:
        super().__init__(reason_code)
        self.status = status
        self.reason_code = reason_code


def _base_status(path: Path) -> dict[str, Any]:
    return {
        "protocol_version": STATUS_PROTOCOL_VERSION,
        "status": "inspection_failed",
        "initialized": False,
        "ready": False,
        "runtime_state": "inspection_failed",
        "reason_code": "inspection_failed",
        "readonly": True,
        "sidecar": {
            "path": str(path.resolve()),
            "schema_version": None,
            "runtime_profile_identity": None,
            "initialized_at": None,
        },
        "identities": dict(EXPECTED_IDENTITIES),
        "runtime_profile": dict(EXPECTED_RUNTIME_PROFILE),
        "counts": {key: 0 for key in _COUNT_TABLES},
        "integrity": {
            "integrity_check": "not_run",
            "quick_check": "not_run",
            "foreign_key_violations": None,
            "append_only": False,
            "trigger_count": 0,
        },
        "execution": {
            "credential_read": False,
            "sent_to_model": False,
            "external_model_calls": False,
        },
    }


def _failure(path: Path, status: str, reason_code: str) -> dict[str, Any]:
    result = _base_status(path)
    result.update(
        status=status,
        runtime_state="not_initialized" if status == "missing" else status,
        reason_code=reason_code,
    )
    return result


def _connect_readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _pragma_result(connection: sqlite3.Connection, name: str) -> str:
    rows = [str(row[0]) for row in connection.execute(f"PRAGMA {name}").fetchall()]
    return "ok" if rows == ["ok"] else "; ".join(rows[:3]) or "failed"


def _verify_integrity(connection: sqlite3.Connection, result: dict[str, Any]) -> None:
    integrity_check = _pragma_result(connection, "integrity_check")
    quick_check = _pragma_result(connection, "quick_check")
    foreign_key_violations = len(connection.execute("PRAGMA foreign_key_check").fetchall())
    result["integrity"].update(
        integrity_check=integrity_check,
        quick_check=quick_check,
        foreign_key_violations=foreign_key_violations,
    )
    if integrity_check != "ok" or quick_check != "ok" or foreign_key_violations:
        raise _StatusProblem("integrity_failure", "database_integrity_failure")


def _verify_schema(connection: sqlite3.Connection, result: dict[str, Any]) -> None:
    tables = {
        str(row[0]) for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    if not set(_EXPECTED_COLUMNS).issubset(tables):
        raise _StatusProblem("incompatible", "runtime_schema_incompatible")

    for table, expected_columns in _EXPECTED_COLUMNS.items():
        columns = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
        if columns != expected_columns:
            raise _StatusProblem("incompatible", "runtime_schema_incompatible")

    schema_rows = connection.execute(
        "SELECT schema_version, runtime_profile_json, runtime_profile_identity, initialized_at "
        "FROM runtime_schema"
    ).fetchall()
    if len(schema_rows) != 1:
        raise _StatusProblem("incompatible", "runtime_schema_incompatible")
    schema = schema_rows[0]
    result["initialized"] = True
    result["sidecar"].update(
        schema_version=schema["schema_version"],
        runtime_profile_identity=schema["runtime_profile_identity"],
        initialized_at=schema["initialized_at"],
    )
    try:
        profile = json.loads(schema["runtime_profile_json"])
    except (TypeError, json.JSONDecodeError):
        raise _StatusProblem("incompatible", "runtime_profile_metadata_malformed") from None
    if schema["schema_version"] != EXPECTED_IDENTITIES["runtime_schema"]:
        raise _StatusProblem("incompatible", "runtime_schema_incompatible")
    if profile != EXPECTED_RUNTIME_PROFILE or schema["runtime_profile_identity"] != runtime_profile_identity():
        raise _StatusProblem("incompatible", "runtime_profile_binding_mismatch")


def _verify_append_only(connection: sqlite3.Connection, result: dict[str, Any]) -> None:
    trigger_count = 0
    for table in _EXPECTED_COLUMNS:
        rows = connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?",
            (table,),
        ).fetchall()
        by_name = {str(row["name"]): " ".join(str(row["sql"] or "").split()).upper() for row in rows}
        for action in ("UPDATE", "DELETE"):
            name = f"{table}_no_{action.lower()}"
            sql = by_name.get(name, "")
            expected_fragments = (
                f"BEFORE {action} ON {table}".upper(),
                f"RAISE(ABORT, '{table} IS APPEND-ONLY')".upper(),
            )
            if not all(fragment in sql for fragment in expected_fragments):
                raise _StatusProblem("incompatible", "append_only_protection_missing")
            trigger_count += 1
    result["integrity"].update(append_only=True, trigger_count=trigger_count)


def _read_and_validate_ledger(connection: sqlite3.Connection, result: dict[str, Any]) -> None:
    snapshot: dict[str, list[dict[str, Any]]] = {}
    for label, table in _COUNT_TABLES.items():
        rows = [dict(row) for row in connection.execute(f"SELECT * FROM {table}").fetchall()]
        snapshot[table] = rows
        result["counts"][label] = len(rows)
    try:
        validated = validate_runtime_ledger(snapshot)
    except RuntimeProtocolContractError as exc:
        raise _StatusProblem("incompatible", exc.reason_code) from None
    result["execution"].update(
        sent_to_model=validated["sent_to_model"],
        external_model_calls=validated["external_model_calls"],
    )


def _read_fixed_production_status() -> dict[str, Any]:
    path = PRODUCTION_RUNTIME_SIDECAR
    result = _base_status(path)
    try:
        with closing(_connect_readonly(path)) as connection:
            _verify_integrity(connection, result)
            _verify_schema(connection, result)
            _verify_append_only(connection, result)
            _read_and_validate_ledger(connection, result)
    except _StatusProblem as exc:
        result.update(status=exc.status, ready=False, runtime_state=exc.status, reason_code=exc.reason_code)
        return result
    except sqlite3.DatabaseError as exc:
        detail = str(exc).lower()
        if any(marker in detail for marker in ("malformed", "not a database", "disk image")):
            return _failure(path, "integrity_failure", "database_malformed")
        return _failure(path, "inspection_failed", "inspection_failed")
    except OSError:
        return _failure(path, "inspection_failed", "inspection_failed")

    has_runs = result["counts"]["model_runs"] > 0
    result.update(
        status="ready",
        initialized=True,
        ready=True,
        runtime_state="initialized_with_runs" if has_runs else "initialized_zero_run",
        reason_code="ready_with_runs" if has_runs else "ready_zero_run",
    )
    return result


def production_runtime_status() -> dict[str, Any]:
    """Inspect only the fixed production Runtime sidecar without credentials or network I/O."""
    path = PRODUCTION_RUNTIME_SIDECAR
    if not path.is_file():
        return _failure(path, "missing", "sidecar_not_initialized")
    return _read_fixed_production_status()


__all__ = [
    "EXPECTED_IDENTITIES",
    "EXPECTED_RUNTIME_PROFILE",
    "PRODUCTION_RUNTIME_SIDECAR",
    "STATUS_PROTOCOL_VERSION",
    "production_runtime_status",
]
