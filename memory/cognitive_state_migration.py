from __future__ import annotations

import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from memory.db_path_policy import (
    KnowledgeSidecarPathError,
    connect_alias_safe_test_database,
)
from memory.operational_state_models import (
    OperationalStateError,
    normalize_timestamp,
    utc_now,
)
from memory.operational_state_store import (
    SCHEMA_MARKER,
    SCHEMA_VERSION,
    OperationalStateStore,
    is_production_operational_path,
    schema_fingerprint,
)


COGNITIVE_SCHEMA_GENERATION = 10001
COGNITIVE_SCHEMA_MARKER = "yunos-bounded-cognitive-agency-10001"
COGNITIVE_CONTRACT_VERSION = 1
COGNITIVE_SCHEMA_RESOURCE = Path(__file__).with_name(
    "cognitive_schema_10001.sql"
)

_COGNITIVE_OBJECTS = frozenset(
    {
        ("table", "cognitive_schema"),
        ("table", "objective_contracts"),
        ("table", "provider_registry"),
        ("table", "capability_registry"),
        ("table", "plan_graphs"),
        ("table", "plan_nodes"),
        ("table", "plan_edges"),
        ("table", "plan_revisions"),
        ("table", "context_bindings"),
        ("table", "authorization_artifacts"),
        ("table", "cognitive_runs"),
        ("table", "cognitive_run_checkpoints"),
        ("table", "tool_proposals"),
        ("table", "tool_executions"),
        ("trigger", "objective_contracts_no_update"),
        ("trigger", "objective_contracts_no_delete"),
        ("trigger", "provider_registry_no_update"),
        ("trigger", "provider_registry_no_delete"),
        ("trigger", "capability_registry_no_update"),
        ("trigger", "capability_registry_no_delete"),
        ("trigger", "plan_graphs_no_update"),
        ("trigger", "plan_graphs_no_delete"),
        ("trigger", "plan_nodes_no_update"),
        ("trigger", "plan_nodes_no_delete"),
        ("trigger", "plan_edges_no_update"),
        ("trigger", "plan_edges_no_delete"),
        ("trigger", "plan_revisions_no_update"),
        ("trigger", "plan_revisions_no_delete"),
        ("trigger", "authorization_artifacts_no_update"),
        ("trigger", "authorization_artifacts_no_delete"),
        ("trigger", "context_bindings_no_update"),
        ("trigger", "context_bindings_no_delete"),
        ("trigger", "cognitive_run_checkpoints_no_update"),
        ("trigger", "cognitive_run_checkpoints_no_delete"),
        ("trigger", "tool_proposals_no_update"),
        ("trigger", "tool_proposals_no_delete"),
        ("trigger", "tool_executions_no_update"),
        ("trigger", "tool_executions_no_delete"),
        ("index", "objective_contracts_project_order"),
        ("index", "plan_graphs_objective_order"),
        ("index", "plan_edges_outgoing"),
        ("index", "context_bindings_objective_source"),
        ("index", "cognitive_runs_status_activity"),
        ("index", "tool_proposals_run_status"),
        ("index", "tool_executions_run_status"),
    }
)
_COGNITIVE_OBJECT_NAMES = frozenset(
    name for _object_type, name in _COGNITIVE_OBJECTS
)
_CREATE_OBJECT_PATTERN = re.compile(
    r"^CREATE\s+(TABLE|TRIGGER|INDEX)\s+"
    r"([A-Za-z_][A-Za-z0-9_]*)\b",
    flags=re.IGNORECASE,
)


def _comments_only(fragment: str) -> bool:
    without_comments = re.sub(
        r"--[^\r\n]*(?:\r?\n|$)|/\*.*?\*/",
        "",
        fragment,
        flags=re.DOTALL,
    )
    return not without_comments.strip()


def _parse_sql_statements(sql_text: str) -> tuple[str, ...]:
    statements: list[str] = []
    pending: list[str] = []
    for character in sql_text:
        pending.append(character)
        if character != ";":
            continue
        candidate = "".join(pending)
        if sqlite3.complete_statement(candidate):
            statements.append(candidate.strip())
            pending.clear()
    remainder = "".join(pending)
    if remainder and not _comments_only(remainder):
        raise OperationalStateError("cognitive_schema_resource_incomplete")
    return tuple(statements)


def _without_leading_comments(statement: str) -> str:
    remainder = statement.lstrip()
    while remainder:
        if remainder.startswith("--"):
            newline = remainder.find("\n")
            if newline < 0:
                return ""
            remainder = remainder[newline + 1 :].lstrip()
            continue
        if remainder.startswith("/*"):
            end = remainder.find("*/", 2)
            if end < 0:
                return ""
            remainder = remainder[end + 2 :].lstrip()
            continue
        break
    return remainder


def _load_cognitive_ddl() -> tuple[str, ...]:
    try:
        sql_text = COGNITIVE_SCHEMA_RESOURCE.read_text(encoding="utf-8")
    except OSError as exc:
        raise OperationalStateError(
            "cognitive_schema_resource_unavailable"
        ) from exc
    statements = _parse_sql_statements(sql_text)
    objects: set[tuple[str, str]] = set()
    for statement in statements:
        match = _CREATE_OBJECT_PATTERN.match(
            _without_leading_comments(statement)
        )
        if match is None:
            raise OperationalStateError(
                "cognitive_schema_resource_object_mismatch"
            )
        identity = (match.group(1).lower(), match.group(2))
        if identity in objects:
            raise OperationalStateError(
                "cognitive_schema_resource_object_mismatch"
            )
        objects.add(identity)
    if len(statements) != 45 or objects != _COGNITIVE_OBJECTS:
        raise OperationalStateError(
            "cognitive_schema_resource_object_mismatch"
        )
    return statements


_COGNITIVE_DDL = _load_cognitive_ddl()


def _open_migration_connection(
    store: OperationalStateStore,
) -> sqlite3.Connection:
    try:
        connection = connect_alias_safe_test_database(
            store.path,
            timeout=5.0,
        )
    except KnowledgeSidecarPathError as exc:
        raise OperationalStateError(
            "unsafe_operational_database_target"
        ) from exc
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def _existing_cognitive_objects(
    connection: sqlite3.Connection,
) -> set[tuple[str, str]]:
    placeholders = ",".join("?" for _ in _COGNITIVE_OBJECT_NAMES)
    rows = connection.execute(
        f"SELECT type,name FROM sqlite_master WHERE name IN({placeholders})",
        tuple(sorted(_COGNITIVE_OBJECT_NAMES)),
    ).fetchall()
    return {(str(row[0]), str(row[1])) for row in rows}


def _all_named_schema_objects(
    connection: sqlite3.Connection,
) -> set[tuple[str, str]]:
    rows = connection.execute(
        "SELECT type,name FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {(str(row[0]), str(row[1])) for row in rows}


def _verify_cognitive_connection(
    connection: sqlite3.Connection,
) -> dict[str, Any]:
    try:
        row = connection.execute(
            "SELECT marker,schema_generation,schema_digest,contract_version,"
            "created_at,writer_default_enabled "
            "FROM cognitive_schema WHERE singleton=1"
        ).fetchone()
        operational = connection.execute(
            "SELECT marker,schema_version,schema_digest "
            "FROM operational_schema WHERE singleton=1"
        ).fetchone()
    except sqlite3.DatabaseError:
        raise OperationalStateError("cognitive_schema_missing") from None
    if row is None:
        raise OperationalStateError("cognitive_schema_missing")
    if operational is None:
        raise OperationalStateError("operational_schema_missing")
    if (
        row["marker"] != COGNITIVE_SCHEMA_MARKER
        or row["schema_generation"] != COGNITIVE_SCHEMA_GENERATION
        or row["contract_version"] != COGNITIVE_CONTRACT_VERSION
        or row["writer_default_enabled"] != 0
    ):
        raise OperationalStateError("cognitive_schema_marker_mismatch")
    if (
        operational["marker"] != SCHEMA_MARKER
        or operational["schema_version"] != SCHEMA_VERSION
    ):
        raise OperationalStateError("operational_schema_marker_mismatch")
    objects = _existing_cognitive_objects(connection)
    if objects != _COGNITIVE_OBJECTS:
        raise OperationalStateError("cognitive_schema_incomplete")
    digest = schema_fingerprint(connection)
    if row["schema_digest"] != digest:
        raise OperationalStateError("cognitive_schema_drift")
    if operational["schema_digest"] != digest:
        raise OperationalStateError("operational_schema_drift")
    return {
        "marker": row["marker"],
        "schema_generation": row["schema_generation"],
        "schema_digest": digest,
        "contract_version": row["contract_version"],
        "created_at": row["created_at"],
        "writer_default_enabled": False,
        "operational_marker": operational["marker"],
        "operational_schema_version": operational["schema_version"],
    }


def apply_cognitive_state_migration(
    path: str | os.PathLike[str],
    *,
    created_at: str | None = None,
) -> dict[str, Any]:
    target = Path(path).expanduser().resolve()
    if is_production_operational_path(target):
        raise OperationalStateError("cognitive_production_migration_forbidden")
    timestamp = normalize_timestamp(created_at or utc_now())
    store = OperationalStateStore(target)
    with closing(_open_migration_connection(store)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            if connection.execute(
                "SELECT 1 FROM state_writer_leases LIMIT 1"
            ).fetchone() is not None:
                raise OperationalStateError(
                    "cognitive_migration_active_writer_lease"
                )
            existing = _existing_cognitive_objects(connection)
            if existing:
                if existing != _COGNITIVE_OBJECTS:
                    raise OperationalStateError(
                        "cognitive_partial_schema_detected"
                    )
            OperationalStateStore._verify_schema(connection)
            if existing:
                result = _verify_cognitive_connection(connection)
                connection.rollback()
                return {**result, "idempotent_replay": True}

            objects_before = _all_named_schema_objects(connection)
            for statement in _COGNITIVE_DDL:
                connection.execute(statement)
            objects_after = _all_named_schema_objects(connection)
            if objects_after - objects_before != _COGNITIVE_OBJECTS:
                raise OperationalStateError(
                    "cognitive_schema_resource_effect_mismatch"
                )

            digest = schema_fingerprint(connection)
            connection.execute(
                "UPDATE operational_schema SET schema_digest=? WHERE singleton=1",
                (digest,),
            )
            connection.execute(
                "INSERT INTO cognitive_schema VALUES(?,?,?,?,?,?,?)",
                (
                    1,
                    COGNITIVE_SCHEMA_MARKER,
                    COGNITIVE_SCHEMA_GENERATION,
                    digest,
                    COGNITIVE_CONTRACT_VERSION,
                    timestamp,
                    0,
                ),
            )
            result = _verify_cognitive_connection(connection)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return {**result, "idempotent_replay": False}


def verify_cognitive_state_schema(
    path: str | os.PathLike[str],
) -> dict[str, Any]:
    store = OperationalStateStore(path)
    with closing(store._connect()) as connection:
        return _verify_cognitive_connection(connection)


__all__ = [
    "COGNITIVE_CONTRACT_VERSION",
    "COGNITIVE_SCHEMA_GENERATION",
    "COGNITIVE_SCHEMA_MARKER",
    "COGNITIVE_SCHEMA_RESOURCE",
    "apply_cognitive_state_migration",
    "verify_cognitive_state_schema",
]
