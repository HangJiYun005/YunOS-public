from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from memory.db_path_policy import (
    DEFAULT_OPERATIONAL_STATE_PATH,
    KnowledgeSidecarPathError,
    authorize_alias_safe_test_database_target,
    connect_alias_safe_test_database,
    create_alias_safe_test_database_connection,
)
from memory.operational_state_models import (
    CONTRACT_VERSION,
    ENTITY_TYPES,
    FRESHNESS_STATES,
    PROJECTION_VERSION,
    SCHEMA_VERSION,
    AppendPreview,
    AppendReceipt,
    AuthorityArtifact,
    OperationalStateError,
    SourceAnchor,
    StateEvent,
    build_projection,
    canonical_digest,
    canonical_json,
    normalize_timestamp,
    projection_sort_key,
    utc_now,
    validate_identity,
    validate_sha256,
    validate_transition,
)


PRODUCTION_WRITE_ENV = "YUNOS_OPERATIONAL_STATE_PRODUCTION_WRITE"
WRITER_LEASE_NAME = "operational-state-writer"
SCHEMA_MARKER = "yunos-operational-state-9001"


_SCHEMA_DDL = (
    """
    CREATE TABLE operational_schema(
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        marker TEXT NOT NULL UNIQUE,
        schema_version INTEGER NOT NULL CHECK(schema_version=9001),
        schema_digest TEXT NOT NULL CHECK(length(schema_digest)=64),
        projection_version INTEGER NOT NULL CHECK(projection_version=1),
        created_at TEXT NOT NULL,
        writer_default_enabled INTEGER NOT NULL DEFAULT 0
            CHECK(writer_default_enabled=0)
    )
    """,
    """
    CREATE TABLE state_entities(
        entity_id TEXT PRIMARY KEY,
        entity_type TEXT NOT NULL,
        namespace TEXT NOT NULL,
        external_key TEXT NOT NULL,
        identity_digest TEXT NOT NULL CHECK(length(identity_digest)=64),
        created_event_id TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        UNIQUE(entity_type,namespace,external_key)
    )
    """,
    """
    CREATE TABLE state_source_anchors(
        anchor_id TEXT PRIMARY KEY,
        source_kind TEXT NOT NULL,
        locator TEXT NOT NULL,
        evidence_digest TEXT NOT NULL CHECK(length(evidence_digest)=64),
        observed_at TEXT NOT NULL,
        privacy_scope TEXT NOT NULL,
        availability TEXT NOT NULL DEFAULT 'available',
        canonical_json TEXT NOT NULL,
        anchor_digest TEXT NOT NULL CHECK(length(anchor_digest)=64)
    )
    """,
    """
    CREATE TABLE state_events(
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL UNIQUE,
        project_id TEXT NOT NULL,
        entity_id TEXT NOT NULL REFERENCES state_entities(entity_id),
        entity_type TEXT NOT NULL,
        event_type TEXT NOT NULL,
        source_kind TEXT NOT NULL,
        source_anchor_id TEXT NOT NULL REFERENCES state_source_anchors(anchor_id),
        authority_kind TEXT NOT NULL,
        authority_digest TEXT NOT NULL CHECK(length(authority_digest)=64),
        actor_kind TEXT NOT NULL,
        actor_id TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        effective_at TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        expires_at TEXT,
        supersedes_event_id TEXT REFERENCES state_events(event_id),
        caused_by_event_id TEXT REFERENCES state_events(event_id),
        idempotency_key TEXT NOT NULL UNIQUE,
        conflict_group_id TEXT,
        canonical_payload TEXT NOT NULL,
        payload_digest TEXT NOT NULL CHECK(length(payload_digest)=64),
        privacy_scope TEXT NOT NULL,
        projection_version INTEGER NOT NULL CHECK(projection_version=1),
        schema_version INTEGER NOT NULL CHECK(schema_version=9001)
    )
    """,
    """
    CREATE TRIGGER state_events_no_update
    BEFORE UPDATE ON state_events
    BEGIN SELECT RAISE(ABORT,'state_events_append_only'); END
    """,
    """
    CREATE TRIGGER state_events_no_delete
    BEFORE DELETE ON state_events
    BEGIN SELECT RAISE(ABORT,'state_events_append_only'); END
    """,
    """
    CREATE TABLE state_projections(
        entity_id TEXT PRIMARY KEY REFERENCES state_entities(entity_id),
        project_id TEXT NOT NULL,
        entity_type TEXT NOT NULL,
        state TEXT NOT NULL,
        freshness TEXT NOT NULL,
        current_event_id TEXT NOT NULL REFERENCES state_events(event_id),
        canonical_projection TEXT NOT NULL,
        projection_digest TEXT NOT NULL CHECK(length(projection_digest)=64),
        projection_version INTEGER NOT NULL CHECK(projection_version=1),
        rebuilt_through_sequence INTEGER NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE state_snapshots(
        snapshot_id TEXT PRIMARY KEY,
        through_sequence INTEGER NOT NULL,
        event_chain_digest TEXT NOT NULL CHECK(length(event_chain_digest)=64),
        canonical_snapshot TEXT NOT NULL,
        snapshot_digest TEXT NOT NULL CHECK(length(snapshot_digest)=64),
        projection_version INTEGER NOT NULL CHECK(projection_version=1),
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TRIGGER state_snapshots_no_update
    BEFORE UPDATE ON state_snapshots
    BEGIN SELECT RAISE(ABORT,'state_snapshots_append_only'); END
    """,
    """
    CREATE TRIGGER state_snapshots_no_delete
    BEFORE DELETE ON state_snapshots
    BEGIN SELECT RAISE(ABORT,'state_snapshots_append_only'); END
    """,
    """
    CREATE TABLE state_candidates(
        candidate_id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        entity_id TEXT,
        entity_type TEXT NOT NULL,
        candidate_type TEXT NOT NULL,
        canonical_candidate TEXT NOT NULL,
        candidate_digest TEXT NOT NULL CHECK(length(candidate_digest)=64),
        confidence_ppm INTEGER NOT NULL
            CHECK(confidence_ppm BETWEEN 0 AND 1000000),
        source_anchor_id TEXT NOT NULL REFERENCES state_source_anchors(anchor_id),
        status TEXT NOT NULL
            CHECK(status IN('pending','approved','rejected','expired')),
        expires_at TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE state_conflicts(
        conflict_group_id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        entity_id TEXT NOT NULL REFERENCES state_entities(entity_id),
        field_name TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN('pending','resolved','obsolete')),
        canonical_members TEXT NOT NULL,
        conflict_digest TEXT NOT NULL CHECK(length(conflict_digest)=64),
        resolution_event_id TEXT REFERENCES state_events(event_id),
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE state_approvals(
        approval_id TEXT PRIMARY KEY,
        candidate_id TEXT REFERENCES state_candidates(candidate_id),
        conflict_group_id TEXT REFERENCES state_conflicts(conflict_group_id),
        decision TEXT NOT NULL
            CHECK(decision IN('approve','reject','revoke','resolve','expire')),
        authority_id TEXT NOT NULL,
        authority_digest TEXT NOT NULL CHECK(length(authority_digest)=64),
        canonical_approval TEXT NOT NULL,
        approval_digest TEXT NOT NULL CHECK(length(approval_digest)=64),
        recorded_at TEXT NOT NULL
    )
    """,
    """
    CREATE TRIGGER state_approvals_no_update
    BEFORE UPDATE ON state_approvals
    BEGIN SELECT RAISE(ABORT,'state_approvals_append_only'); END
    """,
    """
    CREATE TRIGGER state_approvals_no_delete
    BEFORE DELETE ON state_approvals
    BEGIN SELECT RAISE(ABORT,'state_approvals_append_only'); END
    """,
    """
    CREATE TABLE state_verifications(
        verification_id TEXT PRIMARY KEY,
        entity_id TEXT NOT NULL REFERENCES state_entities(entity_id),
        event_id TEXT REFERENCES state_events(event_id),
        verifier_kind TEXT NOT NULL,
        result TEXT NOT NULL CHECK(result IN('passed','failed','unverifiable')),
        evidence_digest TEXT NOT NULL CHECK(length(evidence_digest)=64),
        canonical_verification TEXT NOT NULL,
        verification_digest TEXT NOT NULL CHECK(length(verification_digest)=64),
        recorded_at TEXT NOT NULL
    )
    """,
    """
    CREATE TRIGGER state_verifications_no_update
    BEFORE UPDATE ON state_verifications
    BEGIN SELECT RAISE(ABORT,'state_verifications_append_only'); END
    """,
    """
    CREATE TRIGGER state_verifications_no_delete
    BEFORE DELETE ON state_verifications
    BEGIN SELECT RAISE(ABORT,'state_verifications_append_only'); END
    """,
    """
    CREATE TABLE state_writer_leases(
        lease_name TEXT PRIMARY KEY CHECK(lease_name='operational-state-writer'),
        lease_token_digest TEXT NOT NULL CHECK(length(lease_token_digest)=64),
        owner_id TEXT NOT NULL,
        acquired_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE state_lease_events(
        lease_event_id TEXT PRIMARY KEY,
        action TEXT NOT NULL CHECK(action IN('acquired','released','recovered')),
        lease_token_digest TEXT NOT NULL CHECK(length(lease_token_digest)=64),
        owner_id TEXT NOT NULL,
        effective_at TEXT NOT NULL,
        authority_digest TEXT,
        canonical_event TEXT NOT NULL,
        event_digest TEXT NOT NULL CHECK(length(event_digest)=64)
    )
    """,
    """
    CREATE TRIGGER state_lease_events_no_update
    BEFORE UPDATE ON state_lease_events
    BEGIN SELECT RAISE(ABORT,'state_lease_events_append_only'); END
    """,
    """
    CREATE TRIGGER state_lease_events_no_delete
    BEFORE DELETE ON state_lease_events
    BEGIN SELECT RAISE(ABORT,'state_lease_events_append_only'); END
    """,
    """
    CREATE TABLE delegation_contracts(
        delegation_id TEXT PRIMARY KEY REFERENCES state_entities(entity_id),
        project_id TEXT NOT NULL,
        canonical_contract TEXT NOT NULL,
        contract_digest TEXT NOT NULL CHECK(length(contract_digest)=64),
        authority_digest TEXT NOT NULL CHECK(length(authority_digest)=64),
        valid_from TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        created_event_id TEXT NOT NULL REFERENCES state_events(event_id)
    )
    """,
    """
    CREATE TRIGGER delegation_contracts_no_update
    BEFORE UPDATE ON delegation_contracts
    BEGIN SELECT RAISE(ABORT,'delegation_contracts_append_only'); END
    """,
    """
    CREATE TRIGGER delegation_contracts_no_delete
    BEFORE DELETE ON delegation_contracts
    BEGIN SELECT RAISE(ABORT,'delegation_contracts_append_only'); END
    """,
    """
    CREATE TABLE outcome_records(
        outcome_id TEXT PRIMARY KEY REFERENCES state_entities(entity_id),
        delegation_id TEXT NOT NULL REFERENCES delegation_contracts(delegation_id),
        runtime_run_id TEXT,
        receipt_digest TEXT,
        verification_digest TEXT,
        artifact_references TEXT NOT NULL,
        status TEXT NOT NULL
            CHECK(status IN('reported','verified','accepted','rejected','superseded')),
        canonical_outcome TEXT NOT NULL,
        outcome_digest TEXT NOT NULL CHECK(length(outcome_digest)=64),
        current_event_id TEXT NOT NULL REFERENCES state_events(event_id)
    )
    """,
    """
    CREATE TABLE state_receipts(
        receipt_id TEXT PRIMARY KEY,
        idempotency_key TEXT NOT NULL UNIQUE,
        event_id TEXT NOT NULL REFERENCES state_events(event_id),
        authority_digest TEXT NOT NULL CHECK(length(authority_digest)=64),
        preview_digest TEXT NOT NULL CHECK(length(preview_digest)=64),
        projection_digest TEXT NOT NULL CHECK(length(projection_digest)=64),
        sequence INTEGER NOT NULL,
        recorded_at TEXT NOT NULL,
        canonical_receipt TEXT NOT NULL,
        receipt_digest TEXT NOT NULL CHECK(length(receipt_digest)=64)
    )
    """,
    """
    CREATE TRIGGER state_receipts_no_update
    BEFORE UPDATE ON state_receipts
    BEGIN SELECT RAISE(ABORT,'state_receipts_append_only'); END
    """,
    """
    CREATE TRIGGER state_receipts_no_delete
    BEFORE DELETE ON state_receipts
    BEGIN SELECT RAISE(ABORT,'state_receipts_append_only'); END
    """,
    """
    CREATE INDEX state_events_entity_order
    ON state_events(entity_id,effective_at,recorded_at,event_id)
    """,
    """
    CREATE INDEX state_events_type_order
    ON state_events(entity_type,event_type,recorded_at,event_id)
    """,
    """
    CREATE INDEX state_events_project_order
    ON state_events(project_id,recorded_at,event_id)
    """,
    """
    CREATE INDEX state_candidates_status_expiry
    ON state_candidates(status,expires_at,candidate_id)
    """,
    """
    CREATE INDEX state_conflicts_status
    ON state_conflicts(status,conflict_group_id)
    """,
)


@dataclass(frozen=True)
class Lease:
    token_digest: str
    owner_id: str
    acquired_at: str
    expires_at: str


@dataclass(frozen=True)
class IntegrityReport:
    integrity_check: str
    foreign_key_violations: int
    schema_version: int
    schema_digest: str
    schema_drift: bool


def _absolute(path: str | os.PathLike[str]) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
        os.path.abspath(right)
    )


def is_production_operational_path(path: str | os.PathLike[str]) -> bool:
    target = _absolute(path)
    production = _absolute(DEFAULT_OPERATIONAL_STATE_PATH)
    if _same_path(target, production):
        return True
    if target.exists() and production.exists():
        try:
            return os.path.samefile(target, production)
        except OSError as exc:
            raise OperationalStateError(
                "operational_database_identity_unverifiable"
            ) from exc
    return False


def _production_write_allowed(allow_production: bool) -> bool:
    return (
        allow_production is True
        and os.environ.get(PRODUCTION_WRITE_ENV, "").strip() == "1"
    )


def _schema_rows(connection: sqlite3.Connection) -> list[dict[str, str]]:
    rows = connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name,tbl_name"
    ).fetchall()
    return [
        {
            "type": row[0],
            "name": row[1],
            "table": row[2],
            "sql": " ".join((row[3] or "").split()),
        }
        for row in rows
    ]


def schema_fingerprint(connection: sqlite3.Connection) -> str:
    return canonical_digest(_schema_rows(connection))


def _configure_connection(connection: sqlite3.Connection) -> None:
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")


def _initialize_connection(connection: sqlite3.Connection, created_at: str) -> None:
    _configure_connection(connection)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("BEGIN IMMEDIATE")
    try:
        for statement in _SCHEMA_DDL:
            connection.execute(statement)
        digest = schema_fingerprint(connection)
        connection.execute(
            "INSERT INTO operational_schema VALUES(?,?,?,?,?,?,?)",
            (
                1,
                SCHEMA_MARKER,
                SCHEMA_VERSION,
                digest,
                PROJECTION_VERSION,
                normalize_timestamp(created_at),
                0,
            ),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def initialize_operational_database(
    path: str | os.PathLike[str],
    *,
    created_at: str | None = None,
    allow_production: bool = False,
) -> Path:
    target = _absolute(path)
    timestamp = normalize_timestamp(created_at or utc_now())
    production = is_production_operational_path(target)
    if production:
        if not _production_write_allowed(allow_production):
            raise OperationalStateError("production_writer_disabled")
        if target.exists():
            raise OperationalStateError("operational_database_already_exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor: int | None = None
        connection: sqlite3.Connection | None = None
        failed = True
        try:
            descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            os.close(descriptor)
            descriptor = None
            connection = sqlite3.connect(target)
            _initialize_connection(connection, timestamp)
            failed = False
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if connection is not None:
                connection.close()
            if failed and target.exists():
                target.unlink()
        return target

    try:
        with create_alias_safe_test_database_connection(target) as connection:
            _initialize_connection(connection, timestamp)
    except KnowledgeSidecarPathError as exc:
        raise OperationalStateError("unsafe_operational_database_target") from exc
    return target


def _decode_object(raw: str, reason_code: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        raise OperationalStateError(reason_code) from None
    if not isinstance(value, dict) or canonical_json(value) != raw:
        raise OperationalStateError(reason_code)
    return value


class OperationalStateStore:
    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        allow_production_write: bool = False,
    ) -> None:
        self.path = _absolute(path)
        self.production = is_production_operational_path(self.path)
        self.allow_production_write = allow_production_write
        if not self.production:
            try:
                authorize_alias_safe_test_database_target(
                    self.path, require_exists=True
                )
            except KnowledgeSidecarPathError as exc:
                raise OperationalStateError(
                    "unsafe_operational_database_target"
                ) from exc

    def _connect(self, *, write: bool = False) -> sqlite3.Connection:
        if not self.path.is_file():
            raise OperationalStateError("operational_database_missing")
        if write and self.production and not _production_write_allowed(
            self.allow_production_write
        ):
            raise OperationalStateError("production_writer_disabled")
        if self.production:
            mode = "rw" if write else "ro"
            connection = sqlite3.connect(
                f"file:{self.path.as_posix()}?mode={mode}",
                uri=True,
                timeout=5.0,
            )
        else:
            try:
                connection = connect_alias_safe_test_database(
                    self.path,
                    timeout=5.0,
                )
            except KnowledgeSidecarPathError as exc:
                raise OperationalStateError(
                    "unsafe_operational_database_target"
                ) from exc
        _configure_connection(connection)
        if not write:
            connection.execute("PRAGMA query_only=ON")
        self._verify_schema(connection)
        return connection

    @staticmethod
    def _verify_schema(connection: sqlite3.Connection) -> None:
        try:
            row = connection.execute(
                "SELECT marker,schema_version,schema_digest,projection_version,"
                "writer_default_enabled FROM operational_schema WHERE singleton=1"
            ).fetchone()
        except sqlite3.DatabaseError:
            raise OperationalStateError("operational_schema_missing") from None
        if row is None:
            raise OperationalStateError("operational_schema_missing")
        if (
            row["marker"] != SCHEMA_MARKER
            or row["schema_version"] != SCHEMA_VERSION
            or row["projection_version"] != PROJECTION_VERSION
            or row["writer_default_enabled"] != 0
        ):
            raise OperationalStateError("operational_schema_marker_mismatch")
        if row["schema_digest"] != schema_fingerprint(connection):
            raise OperationalStateError("operational_schema_drift")

    def integrity_report(self) -> IntegrityReport:
        with closing(self._connect()) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            fk = len(connection.execute("PRAGMA foreign_key_check").fetchall())
            row = connection.execute(
                "SELECT schema_version,schema_digest FROM operational_schema"
            ).fetchone()
            current = schema_fingerprint(connection)
        return IntegrityReport(
            integrity_check=integrity,
            foreign_key_violations=fk,
            schema_version=row["schema_version"],
            schema_digest=row["schema_digest"],
            schema_drift=row["schema_digest"] != current,
        )

    def read_status(self) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            marker = connection.execute(
                "SELECT marker,schema_version,schema_digest,projection_version,"
                "writer_default_enabled,created_at FROM operational_schema"
            ).fetchone()
            counts = {}
            for table in (
                "state_entities",
                "state_events",
                "state_projections",
                "state_snapshots",
                "state_candidates",
                "state_conflicts",
                "state_approvals",
                "state_verifications",
                "state_writer_leases",
                "state_receipts",
                "delegation_contracts",
                "outcome_records",
            ):
                counts[table] = connection.execute(
                    f'SELECT COUNT(*) FROM "{table}"'
                ).fetchone()[0]
            lease = connection.execute(
                "SELECT owner_id,acquired_at,expires_at FROM state_writer_leases "
                "WHERE lease_name=?",
                (WRITER_LEASE_NAME,),
            ).fetchone()
        return {
            "status": "ready",
            "database_kind": "operational_state",
            "path_kind": "production" if self.production else "test",
            "marker": marker["marker"],
            "schema_version": marker["schema_version"],
            "schema_digest": marker["schema_digest"],
            "projection_version": marker["projection_version"],
            "writer_default_enabled": bool(marker["writer_default_enabled"]),
            "writer_enabled_for_process": bool(
                self.production
                and _production_write_allowed(self.allow_production_write)
            ),
            "created_at": marker["created_at"],
            "counts": counts,
            "lease": (
                {
                    "owner_id": lease["owner_id"],
                    "acquired_at": lease["acquired_at"],
                    "expires_at": lease["expires_at"],
                }
                if lease
                else None
            ),
        }

    @staticmethod
    def _lease_event(
        connection: sqlite3.Connection,
        *,
        action: str,
        token_digest: str,
        owner_id: str,
        effective_at: str,
        authority_digest: str | None = None,
    ) -> None:
        payload = {
            "action": action,
            "lease_token_digest": token_digest,
            "owner_id": owner_id,
            "effective_at": effective_at,
            "authority_digest": authority_digest,
        }
        event_digest = canonical_digest(payload)
        event_id = f"lease-event:sha256:{event_digest}"
        connection.execute(
            "INSERT INTO state_lease_events VALUES(?,?,?,?,?,?,?,?)",
            (
                event_id,
                action,
                token_digest,
                owner_id,
                effective_at,
                authority_digest,
                canonical_json(payload),
                event_digest,
            ),
        )

    def acquire_writer_lease(
        self,
        *,
        owner_id: str,
        token: str,
        acquired_at: str,
        expires_at: str,
    ) -> Lease:
        validate_identity(owner_id, "invalid_lease_owner")
        if not isinstance(token, str) or len(token) < 16:
            raise OperationalStateError("invalid_lease_token")
        acquired = normalize_timestamp(acquired_at)
        expires = normalize_timestamp(expires_at)
        if expires <= acquired:
            raise OperationalStateError("invalid_lease_interval")
        token_digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with closing(self._connect(write=True)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM state_writer_leases WHERE lease_name=?",
                (WRITER_LEASE_NAME,),
            ).fetchone()
            if existing is not None:
                connection.rollback()
                if existing["expires_at"] <= acquired:
                    raise OperationalStateError(
                        "expired_lease_requires_recovery"
                    )
                raise OperationalStateError("writer_lease_conflict")
            connection.execute(
                "INSERT INTO state_writer_leases VALUES(?,?,?,?,?)",
                (WRITER_LEASE_NAME, token_digest, owner_id, acquired, expires),
            )
            self._lease_event(
                connection,
                action="acquired",
                token_digest=token_digest,
                owner_id=owner_id,
                effective_at=acquired,
            )
            connection.commit()
        return Lease(token_digest, owner_id, acquired, expires)

    def release_writer_lease(
        self,
        *,
        owner_id: str,
        token: str,
        released_at: str,
    ) -> None:
        released = normalize_timestamp(released_at)
        token_digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with closing(self._connect(write=True)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM state_writer_leases WHERE lease_name=?",
                (WRITER_LEASE_NAME,),
            ).fetchone()
            if (
                row is None
                or row["owner_id"] != owner_id
                or row["lease_token_digest"] != token_digest
            ):
                connection.rollback()
                raise OperationalStateError("writer_lease_owner_mismatch")
            connection.execute(
                "DELETE FROM state_writer_leases WHERE lease_name=?",
                (WRITER_LEASE_NAME,),
            )
            self._lease_event(
                connection,
                action="released",
                token_digest=token_digest,
                owner_id=owner_id,
                effective_at=released,
            )
            connection.commit()

    def recover_expired_writer_lease(
        self,
        *,
        recovered_at: str,
        authority_digest: str,
    ) -> dict[str, Any]:
        recovered = normalize_timestamp(recovered_at)
        validate_sha256(authority_digest, "invalid_recovery_authority_digest")
        with closing(self._connect(write=True)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM state_writer_leases WHERE lease_name=?",
                (WRITER_LEASE_NAME,),
            ).fetchone()
            if row is None:
                connection.rollback()
                return {"status": "not_required", "reason_code": "no_writer_lease"}
            if row["expires_at"] > recovered:
                connection.rollback()
                raise OperationalStateError("active_lease_cannot_be_recovered")
            connection.execute(
                "DELETE FROM state_writer_leases WHERE lease_name=?",
                (WRITER_LEASE_NAME,),
            )
            self._lease_event(
                connection,
                action="recovered",
                token_digest=row["lease_token_digest"],
                owner_id=row["owner_id"],
                effective_at=recovered,
                authority_digest=authority_digest,
            )
            connection.commit()
        return {"status": "recovered", "reason_code": "expired_lease_recovered"}

    def _assert_lease(
        self,
        connection: sqlite3.Connection,
        *,
        owner_id: str,
        token: str,
        at: str,
    ) -> None:
        token_digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        row = connection.execute(
            "SELECT * FROM state_writer_leases WHERE lease_name=?",
            (WRITER_LEASE_NAME,),
        ).fetchone()
        if (
            row is None
            or row["owner_id"] != owner_id
            or row["lease_token_digest"] != token_digest
        ):
            raise OperationalStateError("writer_lease_required")
        if row["expires_at"] <= normalize_timestamp(at):
            raise OperationalStateError("writer_lease_expired")

    def _authorize_auxiliary_write(
        self,
        connection: sqlite3.Connection,
        *,
        authority: AuthorityArtifact | None,
        lease_owner_id: str | None,
        lease_token: str | None,
        project_id: str,
        entity_id: str,
        operation: str,
        at: str,
        enforce_for_test: bool = False,
    ) -> None:
        if not self.production and not enforce_for_test:
            return
        if authority is None:
            raise OperationalStateError("production_authority_required")
        authority.authorize_event(
            project_id=project_id,
            entity_id=entity_id,
            event_type=operation,
            at=at,
        )
        if lease_owner_id is None or lease_token is None:
            raise OperationalStateError("production_writer_lease_required")
        self._assert_lease(
            connection,
            owner_id=lease_owner_id,
            token=lease_token,
            at=at,
        )

    @staticmethod
    def _projection_row(
        connection: sqlite3.Connection, entity_id: str
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM state_projections WHERE entity_id=?", (entity_id,)
        ).fetchone()

    @staticmethod
    def _event_exists(
        connection: sqlite3.Connection, event_id: str | None
    ) -> bool:
        if event_id is None:
            return True
        return (
            connection.execute(
                "SELECT 1 FROM state_events WHERE event_id=?", (event_id,)
            ).fetchone()
            is not None
        )

    def _would_win_state(
        self,
        connection: sqlite3.Connection,
        event: StateEvent,
    ) -> bool:
        row = self._projection_row(connection, event.entity_id)
        if row is None:
            return True
        projection = _decode_object(
            row["canonical_projection"], "projection_record_corrupt"
        )
        current_event_id = projection.get("winner_events", {}).get("state")
        if not current_event_id:
            raise OperationalStateError("projection_record_corrupt")
        current = self._load_event(connection, current_event_id)
        return projection_sort_key(event) >= projection_sort_key(current)

    def _validate_event_against_state(
        self,
        connection: sqlite3.Connection,
        event: StateEvent,
    ) -> None:
        if not self._event_exists(connection, event.supersedes_event_id):
            raise OperationalStateError("superseded_event_missing")
        if not self._event_exists(connection, event.caused_by_event_id):
            raise OperationalStateError("causal_event_missing")
        row = self._projection_row(connection, event.entity_id)
        previous_state = row["state"] if row is not None else None
        if self._would_win_state(connection, event):
            validate_transition(
                event.entity_type,
                previous_state,
                event.canonical_payload["state"],
            )

    def preview_event(
        self,
        *,
        authority: AuthorityArtifact,
        event: StateEvent,
    ) -> AppendPreview:
        authority.authorize_event(
            project_id=event.project_id,
            entity_id=event.entity_id,
            event_type=event.event_type,
            at=event.recorded_at,
        )
        event.validate_event_id(authority.authority_digest)
        if event.actor_kind == "agent" and authority.issued_by == event.actor_id:
            raise OperationalStateError("agent_self_authorization")
        with closing(self._connect()) as connection:
            self._validate_event_against_state(connection, event)
            row = self._projection_row(connection, event.entity_id)
            previous = row["projection_digest"] if row is not None else None
        return AppendPreview.create(event, authority.authority_digest, previous)

    @staticmethod
    def _store_anchor(
        connection: sqlite3.Connection, anchor: SourceAnchor
    ) -> None:
        encoded = canonical_json(anchor.to_dict())
        digest = canonical_digest(anchor.to_dict())
        row = connection.execute(
            "SELECT anchor_digest FROM state_source_anchors WHERE anchor_id=?",
            (anchor.anchor_id,),
        ).fetchone()
        if row is not None:
            if row["anchor_digest"] != digest:
                raise OperationalStateError("source_anchor_identity_collision")
            return
        connection.execute(
            "INSERT INTO state_source_anchors VALUES(?,?,?,?,?,?,?,?,?)",
            (
                anchor.anchor_id,
                anchor.source_kind,
                anchor.locator,
                anchor.evidence_digest,
                anchor.observed_at,
                anchor.privacy_scope,
                "available",
                encoded,
                digest,
            ),
        )

    @staticmethod
    def _store_entity(
        connection: sqlite3.Connection, event: StateEvent
    ) -> None:
        row = connection.execute(
            "SELECT entity_type,identity_digest FROM state_entities WHERE entity_id=?",
            (event.entity_id,),
        ).fetchone()
        identity = {
            "entity_id": event.entity_id,
            "entity_type": event.entity_type,
            "namespace": "yunos",
            "external_key": event.entity_id,
        }
        digest = canonical_digest(identity)
        if row is not None:
            if row["entity_type"] != event.entity_type or row["identity_digest"] != digest:
                raise OperationalStateError("entity_identity_collision")
            return
        connection.execute(
            "INSERT INTO state_entities VALUES(?,?,?,?,?,?,?)",
            (
                event.entity_id,
                event.entity_type,
                "yunos",
                event.entity_id,
                digest,
                event.event_id,
                event.recorded_at,
            ),
        )

    def _load_event(
        self, connection: sqlite3.Connection, event_id: str
    ) -> StateEvent:
        row = connection.execute(
            "SELECT * FROM state_events WHERE event_id=?", (event_id,)
        ).fetchone()
        if row is None:
            raise OperationalStateError("event_not_found")
        anchor_row = connection.execute(
            "SELECT canonical_json,anchor_digest FROM state_source_anchors "
            "WHERE anchor_id=?",
            (row["source_anchor_id"],),
        ).fetchone()
        if anchor_row is None:
            raise OperationalStateError("event_anchor_missing")
        anchor_payload = _decode_object(
            anchor_row["canonical_json"], "source_anchor_record_corrupt"
        )
        if canonical_digest(anchor_payload) != anchor_row["anchor_digest"]:
            raise OperationalStateError("source_anchor_record_tampered")
        payload = _decode_object(
            row["canonical_payload"], "event_payload_record_corrupt"
        )
        if canonical_digest(payload) != row["payload_digest"]:
            raise OperationalStateError("event_payload_record_tampered")
        event = StateEvent(
            event_id=row["event_id"],
            project_id=row["project_id"],
            entity_id=row["entity_id"],
            entity_type=row["entity_type"],
            event_type=row["event_type"],
            source_kind=row["source_kind"],
            source_anchor=SourceAnchor.from_dict(anchor_payload),
            authority_kind=row["authority_kind"],
            actor_kind=row["actor_kind"],
            actor_id=row["actor_id"],
            observed_at=row["observed_at"],
            effective_at=row["effective_at"],
            recorded_at=row["recorded_at"],
            expires_at=row["expires_at"],
            supersedes_event_id=row["supersedes_event_id"],
            caused_by_event_id=row["caused_by_event_id"],
            idempotency_key=row["idempotency_key"],
            conflict_group_id=row["conflict_group_id"],
            canonical_payload=payload,
            payload_digest=row["payload_digest"],
            privacy_scope=row["privacy_scope"],
            projection_version=row["projection_version"],
            schema_version=row["schema_version"],
        )
        event.validate_event_id(row["authority_digest"])
        return event

    def _events_for_entity(
        self, connection: sqlite3.Connection, entity_id: str
    ) -> list[tuple[int, StateEvent]]:
        rows = connection.execute(
            "SELECT sequence,event_id FROM state_events WHERE entity_id=? "
            "ORDER BY sequence",
            (entity_id,),
        ).fetchall()
        return [
            (row["sequence"], self._load_event(connection, row["event_id"]))
            for row in rows
        ]

    def _write_projection(
        self,
        connection: sqlite3.Connection,
        entity_id: str,
        *,
        freshness: str | None = None,
    ) -> dict[str, Any]:
        existing = self._projection_row(connection, entity_id)
        selected_freshness = (
            freshness
            if freshness is not None
            else (existing["freshness"] if existing is not None else "fresh")
        )
        events = self._events_for_entity(connection, entity_id)
        projection = build_projection(events, freshness=selected_freshness)
        current_event_id = projection["winner_events"]["state"]
        current_event = next(
            event for _, event in events if event.event_id == current_event_id
        )
        values = (
            projection["entity_id"],
            projection["project_id"],
            projection["entity_type"],
            projection["state"],
            projection["freshness"],
            current_event_id,
            canonical_json(projection),
            projection["projection_digest"],
            PROJECTION_VERSION,
            projection["last_sequence"],
            current_event.recorded_at,
        )
        connection.execute(
            "INSERT INTO state_projections VALUES(?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(entity_id) DO UPDATE SET "
            "project_id=excluded.project_id,entity_type=excluded.entity_type,"
            "state=excluded.state,freshness=excluded.freshness,"
            "current_event_id=excluded.current_event_id,"
            "canonical_projection=excluded.canonical_projection,"
            "projection_digest=excluded.projection_digest,"
            "projection_version=excluded.projection_version,"
            "rebuilt_through_sequence=excluded.rebuilt_through_sequence,"
            "updated_at=excluded.updated_at",
            values,
        )
        return projection

    @staticmethod
    def _receipt_from_row(
        row: sqlite3.Row, *, idempotent_replay: bool
    ) -> AppendReceipt:
        payload = _decode_object(
            row["canonical_receipt"], "receipt_record_corrupt"
        )
        payload["receipt_digest"] = row["receipt_digest"]
        return AppendReceipt.from_dict(
            payload, idempotent_replay=idempotent_replay
        )

    def _commit_preview_on_connection(
        self,
        connection: sqlite3.Connection,
        *,
        authority: AuthorityArtifact,
        preview: AppendPreview,
        lease_owner_id: str,
        lease_token: str,
    ) -> AppendReceipt:
        event = preview.event
        authority.authorize_event(
            project_id=event.project_id,
            entity_id=event.entity_id,
            event_type=event.event_type,
            at=event.recorded_at,
        )
        event.validate_event_id(authority.authority_digest)
        if preview.authority_digest != authority.authority_digest:
            raise OperationalStateError("preview_authority_mismatch")
        existing = connection.execute(
            "SELECT * FROM state_receipts WHERE idempotency_key=?",
            (event.idempotency_key,),
        ).fetchone()
        if existing is not None:
            if (
                existing["preview_digest"] != preview.preview_digest
                or existing["authority_digest"] != authority.authority_digest
                or existing["event_id"] != event.event_id
            ):
                raise OperationalStateError("idempotency_key_collision")
            return self._receipt_from_row(existing, idempotent_replay=True)
        self._assert_lease(
            connection,
            owner_id=lease_owner_id,
            token=lease_token,
            at=event.recorded_at,
        )
        row = self._projection_row(connection, event.entity_id)
        current_digest = row["projection_digest"] if row is not None else None
        if current_digest != preview.previous_projection_digest:
            raise OperationalStateError("stale_append_preview")
        self._validate_event_against_state(connection, event)
        self._store_anchor(connection, event.source_anchor)
        self._store_entity(connection, event)
        try:
            cursor = connection.execute(
                "INSERT INTO state_events("
                "event_id,project_id,entity_id,entity_type,event_type,source_kind,"
                "source_anchor_id,authority_kind,authority_digest,actor_kind,actor_id,"
                "observed_at,effective_at,recorded_at,expires_at,"
                "supersedes_event_id,caused_by_event_id,idempotency_key,"
                "conflict_group_id,canonical_payload,payload_digest,privacy_scope,"
                "projection_version,schema_version"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    event.event_id,
                    event.project_id,
                    event.entity_id,
                    event.entity_type,
                    event.event_type,
                    event.source_kind,
                    event.source_anchor.anchor_id,
                    event.authority_kind,
                    authority.authority_digest,
                    event.actor_kind,
                    event.actor_id,
                    event.observed_at,
                    event.effective_at,
                    event.recorded_at,
                    event.expires_at,
                    event.supersedes_event_id,
                    event.caused_by_event_id,
                    event.idempotency_key,
                    event.conflict_group_id,
                    canonical_json(event.canonical_payload),
                    event.payload_digest,
                    event.privacy_scope,
                    event.projection_version,
                    event.schema_version,
                ),
            )
        except sqlite3.IntegrityError:
            raise OperationalStateError("event_append_conflict") from None
        sequence = cursor.lastrowid
        projection = self._write_projection(connection, event.entity_id)
        receipt = AppendReceipt.create(
            event=event,
            authority_digest=authority.authority_digest,
            preview_digest=preview.preview_digest,
            projection_digest=projection["projection_digest"],
            sequence=sequence,
        )
        connection.execute(
            "INSERT INTO state_receipts VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                receipt.receipt_id,
                receipt.idempotency_key,
                receipt.event_id,
                receipt.authority_digest,
                receipt.preview_digest,
                receipt.projection_digest,
                receipt.sequence,
                receipt.recorded_at,
                canonical_json(receipt.identity_payload()),
                receipt.receipt_digest,
            ),
        )
        return receipt

    def commit_preview(
        self,
        *,
        authority: AuthorityArtifact,
        preview: AppendPreview,
        lease_owner_id: str,
        lease_token: str,
    ) -> AppendReceipt:
        with closing(self._connect(write=True)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                receipt = self._commit_preview_on_connection(
                    connection,
                    authority=authority,
                    preview=preview,
                    lease_owner_id=lease_owner_id,
                    lease_token=lease_token,
                )
            except Exception:
                connection.rollback()
                raise
            if receipt.idempotent_replay:
                connection.rollback()
            else:
                connection.commit()
        return receipt

    def get_projection(self, entity_id: str) -> dict[str, Any] | None:
        validate_identity(entity_id)
        with closing(self._connect()) as connection:
            row = self._projection_row(connection, entity_id)
            if row is None:
                return None
            return self._decode_projection_row(row)

    @staticmethod
    def _decode_projection_row(row: sqlite3.Row) -> dict[str, Any]:
        validate_identity(row["entity_id"])
        projection = _decode_object(
            row["canonical_projection"], "projection_record_corrupt"
        )
        recorded_digest = projection.pop("projection_digest", None)
        expected = canonical_digest(projection)
        projection["projection_digest"] = recorded_digest
        if recorded_digest != expected or row["projection_digest"] != expected:
            raise OperationalStateError("projection_record_tampered")
        return projection

    def list_projections(
        self,
        *,
        entity_type: str | None = None,
        project_id: str | None = None,
        freshness: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        args: list[Any] = []
        if entity_type is not None:
            clauses.append("entity_type=?")
            args.append(entity_type)
        if project_id is not None:
            validate_identity(project_id, "invalid_project_id")
            clauses.append("project_id=?")
            args.append(project_id)
        if freshness is not None:
            if freshness not in FRESHNESS_STATES:
                raise OperationalStateError("invalid_freshness")
            clauses.append("freshness=?")
            args.append(freshness)
        query = "SELECT * FROM state_projections"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY entity_type,entity_id"
        with closing(self._connect()) as connection:
            rows = connection.execute(query, args).fetchall()
        return [self._decode_projection_row(row) for row in rows]

    def list_events(
        self,
        *,
        entity_id: str | None = None,
        project_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        args: list[str] = []
        if entity_id is not None:
            validate_identity(entity_id)
            clauses.append("entity_id=?")
            args.append(entity_id)
        if project_id is not None:
            validate_identity(project_id)
            clauses.append("project_id=?")
            args.append(project_id)
        query = "SELECT sequence,event_id FROM state_events"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY sequence"
        with closing(self._connect()) as connection:
            rows = connection.execute(query, args).fetchall()
            return [
                {
                    "sequence": row["sequence"],
                    "event": self._load_event(
                        connection, row["event_id"]
                    ).to_dict(),
                }
                for row in rows
            ]

    def list_candidates(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        args: list[str] = []
        if project_id is not None:
            validate_identity(project_id, "invalid_project_id")
            clauses.append("project_id=?")
            args.append(project_id)
        if status is not None:
            if status not in {"pending", "approved", "rejected", "expired"}:
                raise OperationalStateError("invalid_candidate_status")
            clauses.append("status=?")
            args.append(status)
        query = "SELECT * FROM state_candidates"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY candidate_id"
        with closing(self._connect()) as connection:
            rows = connection.execute(query, args).fetchall()
            items = []
            for row in rows:
                candidate = _decode_object(
                    row["canonical_candidate"],
                    "candidate_record_corrupt",
                )
                if canonical_digest(candidate) != row["candidate_digest"]:
                    raise OperationalStateError("candidate_record_tampered")
                items.append(
                    {
                        "candidate_id": row["candidate_id"],
                        "project_id": row["project_id"],
                        "entity_id": row["entity_id"],
                        "entity_type": row["entity_type"],
                        "candidate_type": row["candidate_type"],
                        "candidate": candidate,
                        "candidate_digest": row["candidate_digest"],
                        "confidence_ppm": row["confidence_ppm"],
                        "source_anchor_id": row["source_anchor_id"],
                        "status": row["status"],
                        "expires_at": row["expires_at"],
                        "created_at": row["created_at"],
                    }
                )
        return items

    def list_conflicts(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        args: list[str] = []
        if project_id is not None:
            validate_identity(project_id, "invalid_project_id")
            clauses.append("project_id=?")
            args.append(project_id)
        if status is not None:
            if status not in {"pending", "resolved", "obsolete"}:
                raise OperationalStateError("invalid_conflict_status")
            clauses.append("status=?")
            args.append(status)
        query = "SELECT * FROM state_conflicts"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY conflict_group_id"
        with closing(self._connect()) as connection:
            rows = connection.execute(query, args).fetchall()
            items = []
            for row in rows:
                conflict = _decode_object(
                    row["canonical_members"],
                    "conflict_record_corrupt",
                )
                if canonical_digest(conflict) != row["conflict_digest"]:
                    raise OperationalStateError("conflict_record_tampered")
                items.append(
                    {
                        "conflict_group_id": row["conflict_group_id"],
                        "project_id": row["project_id"],
                        "entity_id": row["entity_id"],
                        "field_name": row["field_name"],
                        "status": row["status"],
                        "conflict": conflict,
                        "conflict_digest": row["conflict_digest"],
                        "resolution_event_id": row["resolution_event_id"],
                        "created_at": row["created_at"],
                    }
                )
        return items

    def list_source_evidence(
        self,
        *,
        project_id: str | None = None,
    ) -> list[dict[str, Any]]:
        events = self.list_events(project_id=project_id)
        by_anchor: dict[str, dict[str, Any]] = {}
        for item in events:
            event = item["event"]
            anchor = event["source_anchor"]
            entry = by_anchor.setdefault(
                anchor["anchor_id"],
                {
                    **anchor,
                    "event_ids": [],
                    "authority_kinds": [],
                    "latest_sequence": item["sequence"],
                },
            )
            entry["event_ids"].append(event["event_id"])
            entry["authority_kinds"].append(event["authority_kind"])
            entry["latest_sequence"] = max(
                entry["latest_sequence"], item["sequence"]
            )
        return [
            {
                **entry,
                "event_ids": sorted(set(entry["event_ids"])),
                "authority_kinds": sorted(
                    set(entry["authority_kinds"])
                ),
            }
            for _, entry in sorted(by_anchor.items())
        ]

    def register_delegation_contract(
        self,
        *,
        delegation_id: str,
        project_id: str,
        contract: Mapping[str, Any],
        authority_digest: str,
        valid_from: str,
        expires_at: str,
        created_event_id: str,
        authority: AuthorityArtifact | None = None,
        lease_owner_id: str | None = None,
        lease_token: str | None = None,
    ) -> dict[str, Any]:
        validate_identity(delegation_id, "invalid_delegation_id")
        validate_identity(project_id, "invalid_project_id")
        validate_sha256(authority_digest, "invalid_authority_digest")
        start = normalize_timestamp(valid_from)
        expiry = normalize_timestamp(expires_at)
        if expiry <= start:
            raise OperationalStateError("invalid_delegation_interval")
        encoded = canonical_json(contract)
        digest = canonical_digest(contract)
        with closing(self._connect(write=True)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._authorize_auxiliary_write(
                connection,
                authority=authority,
                lease_owner_id=lease_owner_id,
                lease_token=lease_token,
                project_id=project_id,
                entity_id=delegation_id,
                operation="delegation_contract_register",
                at=start,
            )
            event = connection.execute(
                "SELECT project_id,entity_id,entity_type,authority_digest "
                "FROM state_events WHERE event_id=?",
                (created_event_id,),
            ).fetchone()
            if (
                event is None
                or event["project_id"] != project_id
                or event["entity_id"] != delegation_id
                or event["entity_type"] != "delegation"
                or event["authority_digest"] != authority_digest
            ):
                connection.rollback()
                raise OperationalStateError(
                    "delegation_contract_event_mismatch"
                )
            existing = connection.execute(
                "SELECT contract_digest FROM delegation_contracts "
                "WHERE delegation_id=?",
                (delegation_id,),
            ).fetchone()
            if existing is not None:
                connection.rollback()
                if existing["contract_digest"] != digest:
                    raise OperationalStateError(
                        "delegation_contract_identity_collision"
                    )
                return {
                    "delegation_id": delegation_id,
                    "contract_digest": digest,
                    "idempotent_replay": True,
                }
            connection.execute(
                "INSERT INTO delegation_contracts VALUES(?,?,?,?,?,?,?,?)",
                (
                    delegation_id,
                    project_id,
                    encoded,
                    digest,
                    authority_digest,
                    start,
                    expiry,
                    created_event_id,
                ),
            )
            connection.commit()
        return {
            "delegation_id": delegation_id,
            "contract_digest": digest,
            "idempotent_replay": False,
        }

    def get_delegation_contract(
        self, delegation_id: str
    ) -> dict[str, Any]:
        validate_identity(delegation_id, "invalid_delegation_id")
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM delegation_contracts WHERE delegation_id=?",
                (delegation_id,),
            ).fetchone()
            if row is None:
                raise OperationalStateError("delegation_contract_not_found")
            contract = _decode_object(
                row["canonical_contract"],
                "delegation_contract_record_corrupt",
            )
            if canonical_digest(contract) != row["contract_digest"]:
                raise OperationalStateError(
                    "delegation_contract_record_tampered"
                )
            return {
                "delegation_id": row["delegation_id"],
                "project_id": row["project_id"],
                "contract": contract,
                "contract_digest": row["contract_digest"],
                "authority_digest": row["authority_digest"],
                "valid_from": row["valid_from"],
                "expires_at": row["expires_at"],
                "created_event_id": row["created_event_id"],
            }

    def record_outcome_projection(
        self,
        *,
        outcome_id: str,
        delegation_id: str,
        runtime_run_id: str | None,
        receipt_digest: str | None,
        verification_digest: str | None,
        artifact_references: Sequence[str],
        status: str,
        outcome: Mapping[str, Any],
        current_event_id: str,
        authority: AuthorityArtifact | None = None,
        lease_owner_id: str | None = None,
        lease_token: str | None = None,
        recorded_at: str | None = None,
    ) -> dict[str, Any]:
        validate_identity(outcome_id, "invalid_outcome_id")
        validate_identity(delegation_id, "invalid_delegation_id")
        if runtime_run_id is not None:
            validate_identity(runtime_run_id, "invalid_runtime_run_id")
        if receipt_digest is not None:
            validate_sha256(receipt_digest, "invalid_receipt_digest")
        if verification_digest is not None:
            validate_sha256(
                verification_digest, "invalid_verification_digest"
            )
        if status not in {
            "reported",
            "verified",
            "accepted",
            "rejected",
            "superseded",
        }:
            raise OperationalStateError("invalid_outcome_status")
        references = sorted(set(artifact_references))
        if any(
            not isinstance(reference, str)
            or not reference
            or len(reference) > 512
            for reference in references
        ):
            raise OperationalStateError("invalid_artifact_reference")
        encoded_outcome = canonical_json(outcome)
        outcome_digest = canonical_digest(outcome)
        encoded_references = canonical_json(references)
        timestamp = normalize_timestamp(recorded_at or utc_now())
        with closing(self._connect(write=True)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            contract_row = connection.execute(
                "SELECT project_id FROM delegation_contracts "
                "WHERE delegation_id=?",
                (delegation_id,),
            ).fetchone()
            if contract_row is None:
                connection.rollback()
                raise OperationalStateError("delegation_contract_not_found")
            self._authorize_auxiliary_write(
                connection,
                authority=authority,
                lease_owner_id=lease_owner_id,
                lease_token=lease_token,
                project_id=contract_row["project_id"],
                entity_id=outcome_id,
                operation="outcome_projection_update",
                at=timestamp,
            )
            event = connection.execute(
                "SELECT project_id,entity_id,entity_type,canonical_payload,"
                "payload_digest FROM state_events "
                "WHERE event_id=?",
                (current_event_id,),
            ).fetchone()
            if (
                event is None
                or event["project_id"] != contract_row["project_id"]
                or event["entity_id"] != outcome_id
                or event["entity_type"] != "outcome"
            ):
                connection.rollback()
                raise OperationalStateError("outcome_event_mismatch")
            event_payload = _decode_object(
                event["canonical_payload"], "outcome_event_payload_corrupt"
            )
            if (
                canonical_digest(event_payload) != event["payload_digest"]
                or event_payload.get("state") != status
            ):
                connection.rollback()
                raise OperationalStateError("outcome_event_status_mismatch")
            existing = connection.execute(
                "SELECT delegation_id FROM outcome_records WHERE outcome_id=?",
                (outcome_id,),
            ).fetchone()
            values = (
                delegation_id,
                runtime_run_id,
                receipt_digest,
                verification_digest,
                encoded_references,
                status,
                encoded_outcome,
                outcome_digest,
                current_event_id,
                outcome_id,
            )
            if existing is None:
                connection.execute(
                    "INSERT INTO outcome_records VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        outcome_id,
                        delegation_id,
                        runtime_run_id,
                        receipt_digest,
                        verification_digest,
                        encoded_references,
                        status,
                        encoded_outcome,
                        outcome_digest,
                        current_event_id,
                    ),
                )
            else:
                if existing["delegation_id"] != delegation_id:
                    connection.rollback()
                    raise OperationalStateError(
                        "outcome_identity_collision"
                    )
                connection.execute(
                    "UPDATE outcome_records SET delegation_id=?,"
                    "runtime_run_id=?,receipt_digest=?,verification_digest=?,"
                    "artifact_references=?,status=?,canonical_outcome=?,"
                    "outcome_digest=?,current_event_id=? WHERE outcome_id=?",
                    values,
                )
            connection.commit()
        return {
            "outcome_id": outcome_id,
            "delegation_id": delegation_id,
            "status": status,
            "outcome_digest": outcome_digest,
            "current_event_id": current_event_id,
        }

    def list_outcomes(
        self, *, delegation_id: str | None = None
    ) -> list[dict[str, Any]]:
        args: tuple[str, ...] = ()
        query = "SELECT * FROM outcome_records"
        if delegation_id is not None:
            validate_identity(delegation_id, "invalid_delegation_id")
            query += " WHERE delegation_id=?"
            args = (delegation_id,)
        query += " ORDER BY outcome_id"
        with closing(self._connect()) as connection:
            rows = connection.execute(query, args).fetchall()
            results = []
            for row in rows:
                outcome = _decode_object(
                    row["canonical_outcome"],
                    "outcome_record_corrupt",
                )
                if canonical_digest(outcome) != row["outcome_digest"]:
                    raise OperationalStateError("outcome_record_tampered")
                try:
                    references = json.loads(row["artifact_references"])
                except json.JSONDecodeError:
                    raise OperationalStateError(
                        "outcome_artifact_references_corrupt"
                    ) from None
                if (
                    not isinstance(references, list)
                    or canonical_json(references)
                    != row["artifact_references"]
                ):
                    raise OperationalStateError(
                        "outcome_artifact_references_corrupt"
                    )
                results.append(
                    {
                        "outcome_id": row["outcome_id"],
                        "delegation_id": row["delegation_id"],
                        "runtime_run_id": row["runtime_run_id"],
                        "receipt_digest": row["receipt_digest"],
                        "verification_digest": row[
                            "verification_digest"
                        ],
                        "artifact_references": references,
                        "status": row["status"],
                        "outcome": outcome,
                        "outcome_digest": row["outcome_digest"],
                        "current_event_id": row["current_event_id"],
                    }
                )
        return results

    def rebuild_projections(
        self,
        *,
        snapshot_id: str | None = None,
        authority: AuthorityArtifact | None = None,
        lease_owner_id: str | None = None,
        lease_token: str | None = None,
        recorded_at: str | None = None,
    ) -> dict[str, Any]:
        timestamp = normalize_timestamp(recorded_at or utc_now())
        with closing(self._connect(write=True)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            project_id = (
                authority.target_project_id
                if authority is not None
                else "project:maintenance"
            )
            self._authorize_auxiliary_write(
                connection,
                authority=authority,
                lease_owner_id=lease_owner_id,
                lease_token=lease_token,
                project_id=project_id,
                entity_id=project_id,
                operation="projection_rebuild",
                at=timestamp,
            )
            base_sequence = 0
            if snapshot_id is not None:
                snapshot = self._load_snapshot(connection, snapshot_id)
                base_sequence = snapshot["through_sequence"]
                connection.execute("DELETE FROM state_projections")
                for projection in snapshot["projections"]:
                    projection_copy = dict(projection)
                    digest = projection_copy["projection_digest"]
                    without = dict(projection_copy)
                    without.pop("projection_digest")
                    if canonical_digest(without) != digest:
                        connection.rollback()
                        raise OperationalStateError("snapshot_projection_corrupt")
                    connection.execute(
                        "INSERT INTO state_projections VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            projection_copy["entity_id"],
                            projection_copy["project_id"],
                            projection_copy["entity_type"],
                            projection_copy["state"],
                            projection_copy["freshness"],
                            projection_copy["winner_events"]["state"],
                            canonical_json(projection_copy),
                            digest,
                            PROJECTION_VERSION,
                            projection_copy["last_sequence"],
                            self._load_event(
                                connection,
                                projection_copy["winner_events"]["state"],
                            ).recorded_at,
                        ),
                    )
            else:
                connection.execute("DELETE FROM state_projections")

            if snapshot_id is None:
                entity_rows = connection.execute(
                    "SELECT DISTINCT entity_id FROM state_events ORDER BY entity_id"
                ).fetchall()
            else:
                entity_rows = connection.execute(
                    "SELECT DISTINCT entity_id FROM state_events WHERE sequence>? "
                    "ORDER BY entity_id",
                    (base_sequence,),
                ).fetchall()
            digests: dict[str, str] = {}
            for row in entity_rows:
                projection = self._write_projection(connection, row["entity_id"])
                digests[row["entity_id"]] = projection["projection_digest"]
            connection.commit()
        return {
            "status": "rebuilt",
            "snapshot_id": snapshot_id,
            "base_sequence": base_sequence,
            "projection_count": len(self.list_projections()),
            "rebuilt_entity_digests": digests,
        }

    @staticmethod
    def _event_chain_digest(
        connection: sqlite3.Connection, through_sequence: int
    ) -> str:
        rows = connection.execute(
            "SELECT sequence,event_id,payload_digest FROM state_events "
            "WHERE sequence<=? ORDER BY sequence",
            (through_sequence,),
        ).fetchall()
        return canonical_digest(
            [
                {
                    "sequence": row["sequence"],
                    "event_id": row["event_id"],
                    "payload_digest": row["payload_digest"],
                }
                for row in rows
            ]
        )

    def create_snapshot(
        self,
        *,
        created_at: str,
        authority: AuthorityArtifact | None = None,
        lease_owner_id: str | None = None,
        lease_token: str | None = None,
    ) -> dict[str, Any]:
        timestamp = normalize_timestamp(created_at)
        with closing(self._connect(write=True)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            project_id = (
                authority.target_project_id
                if authority is not None
                else "project:maintenance"
            )
            self._authorize_auxiliary_write(
                connection,
                authority=authority,
                lease_owner_id=lease_owner_id,
                lease_token=lease_token,
                project_id=project_id,
                entity_id=project_id,
                operation="snapshot_create",
                at=timestamp,
            )
            through = connection.execute(
                "SELECT COALESCE(MAX(sequence),0) FROM state_events"
            ).fetchone()[0]
            rows = connection.execute(
                "SELECT canonical_projection FROM state_projections "
                "ORDER BY entity_id"
            ).fetchall()
            projections = [
                _decode_object(
                    row["canonical_projection"], "projection_record_corrupt"
                )
                for row in rows
            ]
            event_chain_digest = self._event_chain_digest(connection, through)
            payload = {
                "contract_version": CONTRACT_VERSION,
                "through_sequence": through,
                "event_chain_digest": event_chain_digest,
                "projections": projections,
                "projection_version": PROJECTION_VERSION,
                "schema_version": SCHEMA_VERSION,
            }
            snapshot_digest = canonical_digest(payload)
            snapshot_id = f"snapshot:sha256:{snapshot_digest}"
            existing = connection.execute(
                "SELECT snapshot_digest FROM state_snapshots WHERE snapshot_id=?",
                (snapshot_id,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO state_snapshots VALUES(?,?,?,?,?,?,?)",
                    (
                        snapshot_id,
                        through,
                        event_chain_digest,
                        canonical_json(payload),
                        snapshot_digest,
                        PROJECTION_VERSION,
                        timestamp,
                    ),
                )
            elif existing["snapshot_digest"] != snapshot_digest:
                connection.rollback()
                raise OperationalStateError("snapshot_identity_collision")
            connection.commit()
        return {
            "snapshot_id": snapshot_id,
            "through_sequence": through,
            "event_chain_digest": event_chain_digest,
            "snapshot_digest": snapshot_digest,
            "projection_count": len(projections),
            "created_at": timestamp,
        }

    def _load_snapshot(
        self, connection: sqlite3.Connection, snapshot_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM state_snapshots WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()
        if row is None:
            raise OperationalStateError("snapshot_not_found")
        payload = _decode_object(
            row["canonical_snapshot"], "snapshot_record_corrupt"
        )
        if canonical_digest(payload) != row["snapshot_digest"]:
            raise OperationalStateError("snapshot_record_tampered")
        if (
            payload.get("through_sequence") != row["through_sequence"]
            or payload.get("event_chain_digest") != row["event_chain_digest"]
            or self._event_chain_digest(connection, row["through_sequence"])
            != row["event_chain_digest"]
        ):
            raise OperationalStateError("snapshot_event_chain_mismatch")
        return payload

    def verify_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        validate_identity(snapshot_id, "invalid_snapshot_id")
        with closing(self._connect()) as connection:
            payload = self._load_snapshot(connection, snapshot_id)
        return {
            "status": "verified",
            "snapshot_id": snapshot_id,
            "through_sequence": payload["through_sequence"],
            "projection_count": len(payload["projections"]),
            "snapshot_digest": canonical_digest(payload),
        }

    def create_candidate(
        self,
        *,
        candidate_id: str,
        project_id: str,
        entity_id: str | None,
        entity_type: str,
        candidate_type: str,
        candidate: Mapping[str, Any],
        confidence_ppm: int,
        source_anchor: SourceAnchor,
        created_at: str,
        expires_at: str | None,
        authority: AuthorityArtifact | None = None,
        lease_owner_id: str | None = None,
        lease_token: str | None = None,
        enforce_authority: bool = False,
        enforce_not_expired_replay: bool = False,
        strict_replay_binding: bool = False,
    ) -> dict[str, Any]:
        validate_identity(candidate_id, "invalid_candidate_id")
        validate_identity(project_id, "invalid_project_id")
        if entity_id is not None:
            validate_identity(entity_id, "invalid_entity_id")
        if entity_type not in ENTITY_TYPES:
            raise OperationalStateError("invalid_entity_type")
        if not isinstance(confidence_ppm, int) or not 0 <= confidence_ppm <= 1_000_000:
            raise OperationalStateError("invalid_candidate_confidence")
        timestamp = normalize_timestamp(created_at)
        expiry = normalize_timestamp(expires_at) if expires_at else None
        encoded = canonical_json(candidate)
        digest = canonical_digest(candidate)
        with closing(self._connect(write=True)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._authorize_auxiliary_write(
                connection,
                authority=authority,
                lease_owner_id=lease_owner_id,
                lease_token=lease_token,
                project_id=project_id,
                entity_id=entity_id or project_id,
                operation="candidate_create",
                at=timestamp,
                enforce_for_test=enforce_authority,
            )
            self._store_anchor(connection, source_anchor)
            row = connection.execute(
                "SELECT * FROM state_candidates "
                "WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()
            if row is not None:
                connection.rollback()
                if (
                    row["candidate_digest"] != digest
                    or (
                        strict_replay_binding
                        and (
                            row["project_id"] != project_id
                            or row["entity_id"] != entity_id
                            or row["entity_type"] != entity_type
                            or row["candidate_type"] != candidate_type
                            or row["confidence_ppm"] != confidence_ppm
                            or row["source_anchor_id"] != source_anchor.anchor_id
                            or row["expires_at"] != expiry
                        )
                    )
                ):
                    raise OperationalStateError("candidate_identity_collision")
                if (
                    enforce_not_expired_replay
                    and row["expires_at"] is not None
                    and row["expires_at"] <= timestamp
                ):
                    raise OperationalStateError("candidate_expired")
                return {
                    "candidate_id": candidate_id,
                    "candidate_digest": digest,
                    "status": row["status"],
                    "idempotent_replay": True,
                }
            connection.execute(
                "INSERT INTO state_candidates VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    candidate_id,
                    project_id,
                    entity_id,
                    entity_type,
                    candidate_type,
                    encoded,
                    digest,
                    confidence_ppm,
                    source_anchor.anchor_id,
                    "pending",
                    expiry,
                    timestamp,
                ),
            )
            connection.commit()
        return {
            "candidate_id": candidate_id,
            "candidate_digest": digest,
            "status": "pending",
            "idempotent_replay": False,
        }

    def decide_candidate(
        self,
        *,
        candidate_id: str,
        decision: str,
        authority: AuthorityArtifact,
        recorded_at: str,
        lease_owner_id: str | None = None,
        lease_token: str | None = None,
        enforce_authority: bool = False,
        enforce_not_expired: bool = False,
        expected_candidate_digest: str | None = None,
        record_validator: Callable[[sqlite3.Connection, str], None] | None = None,
    ) -> dict[str, Any]:
        if decision not in {"approve", "reject", "revoke"}:
            raise OperationalStateError("invalid_candidate_decision")
        timestamp = normalize_timestamp(recorded_at)
        if authority.issued_by.startswith("agent:"):
            raise OperationalStateError("agent_cannot_approve_candidate")
        if expected_candidate_digest is not None:
            validate_sha256(
                expected_candidate_digest,
                "invalid_candidate_digest",
            )
        status = {"approve": "approved", "reject": "rejected", "revoke": "rejected"}[
            decision
        ]
        with closing(self._connect(write=True)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT project_id,entity_id,status,candidate_digest,expires_at "
                "FROM state_candidates "
                "WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise OperationalStateError("candidate_not_found")
            if (
                expected_candidate_digest is not None
                and row["candidate_digest"] != expected_candidate_digest
            ):
                connection.rollback()
                raise OperationalStateError("candidate_digest_mismatch")
            if row["project_id"] != authority.target_project_id:
                connection.rollback()
                raise OperationalStateError("authority_project_mismatch")
            authority.authorize_event(
                project_id=row["project_id"],
                entity_id=row["entity_id"] or row["project_id"],
                event_type="candidate_decision",
                at=timestamp,
            )
            self._authorize_auxiliary_write(
                connection,
                authority=authority,
                lease_owner_id=lease_owner_id,
                lease_token=lease_token,
                project_id=row["project_id"],
                entity_id=row["entity_id"] or row["project_id"],
                operation="candidate_decision",
                at=timestamp,
                enforce_for_test=enforce_authority,
            )
            if (
                enforce_not_expired
                and row["expires_at"] is not None
                and row["expires_at"] <= timestamp
            ):
                connection.rollback()
                raise OperationalStateError("candidate_expired")
            if record_validator is not None:
                try:
                    record_validator(connection, candidate_id)
                except Exception:
                    connection.rollback()
                    raise
            if row["status"] != "pending" and decision != "revoke":
                connection.rollback()
                raise OperationalStateError("candidate_already_decided")
            payload = {
                "candidate_id": candidate_id,
                "candidate_digest": row["candidate_digest"],
                "decision": decision,
                "authority_id": authority.authority_id,
                "authority_digest": authority.authority_digest,
                "recorded_at": timestamp,
            }
            approval_digest = canonical_digest(payload)
            approval_id = f"approval:sha256:{approval_digest}"
            connection.execute(
                "INSERT INTO state_approvals VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    approval_id,
                    candidate_id,
                    None,
                    decision,
                    authority.authority_id,
                    authority.authority_digest,
                    canonical_json(payload),
                    approval_digest,
                    timestamp,
                ),
            )
            connection.execute(
                "UPDATE state_candidates SET status=? WHERE candidate_id=?",
                (status, candidate_id),
            )
            connection.commit()
        return {
            "approval_id": approval_id,
            "candidate_id": candidate_id,
            "decision": decision,
            "status": status,
            "approval_digest": approval_digest,
        }

    def expire_candidates(
        self,
        *,
        as_of: str,
        project_id: str | None = None,
        authority: AuthorityArtifact | None = None,
        lease_owner_id: str | None = None,
        lease_token: str | None = None,
    ) -> dict[str, Any]:
        timestamp = normalize_timestamp(as_of)
        with closing(self._connect(write=True)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            selected_project_id = (
                authority.target_project_id
                if authority is not None
                else (project_id or "project:maintenance")
            )
            if project_id is not None:
                validate_identity(project_id, "invalid_project_id")
                if (
                    authority is not None
                    and project_id != authority.target_project_id
                ):
                    connection.rollback()
                    raise OperationalStateError("authority_project_mismatch")
            self._authorize_auxiliary_write(
                connection,
                authority=authority,
                lease_owner_id=lease_owner_id,
                lease_token=lease_token,
                project_id=selected_project_id,
                entity_id=selected_project_id,
                operation="candidate_expire",
                at=timestamp,
            )
            where = (
                "status='pending' AND expires_at IS NOT NULL AND expires_at<=?"
            )
            values: list[Any] = [timestamp]
            if self.production or project_id is not None:
                where += " AND project_id=?"
                values.append(selected_project_id)
            rows = connection.execute(
                "SELECT candidate_id,project_id,candidate_digest,expires_at "
                f"FROM state_candidates WHERE {where} ORDER BY candidate_id",
                values,
            ).fetchall()
            expired_ids: list[str] = []
            for row in rows:
                approval_authority_id = (
                    authority.authority_id
                    if authority is not None
                    else "system:test-candidate-expiry"
                )
                approval_authority_digest = (
                    authority.authority_digest
                    if authority is not None
                    else "0" * 64
                )
                payload = {
                    "candidate_id": row["candidate_id"],
                    "candidate_digest": row["candidate_digest"],
                    "decision": "expire",
                    "authority_id": approval_authority_id,
                    "authority_digest": approval_authority_digest,
                    "recorded_at": timestamp,
                }
                approval_digest = canonical_digest(payload)
                approval_id = f"approval:sha256:{approval_digest}"
                connection.execute(
                    "INSERT INTO state_approvals VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        approval_id,
                        row["candidate_id"],
                        None,
                        "expire",
                        approval_authority_id,
                        approval_authority_digest,
                        canonical_json(payload),
                        approval_digest,
                        timestamp,
                    ),
                )
                connection.execute(
                    "UPDATE state_candidates SET status='expired' "
                    "WHERE candidate_id=?",
                    (row["candidate_id"],),
                )
                expired_ids.append(row["candidate_id"])
            connection.commit()
        payload = {
            "status": "completed",
            "as_of": timestamp,
            "expired_candidate_ids": expired_ids,
            "expired_count": len(expired_ids),
        }
        return {**payload, "result_digest": canonical_digest(payload)}

    def create_conflict(
        self,
        *,
        conflict_group_id: str,
        project_id: str,
        entity_id: str,
        field_name: str,
        member_event_ids: Sequence[str],
        created_at: str,
        authority: AuthorityArtifact | None = None,
        lease_owner_id: str | None = None,
        lease_token: str | None = None,
    ) -> dict[str, Any]:
        validate_identity(conflict_group_id, "invalid_conflict_group_id")
        timestamp = normalize_timestamp(created_at)
        if len(set(member_event_ids)) < 2:
            raise OperationalStateError("conflict_requires_multiple_members")
        members = sorted(set(member_event_ids))
        payload = {
            "conflict_group_id": conflict_group_id,
            "project_id": project_id,
            "entity_id": entity_id,
            "field_name": field_name,
            "member_event_ids": members,
        }
        digest = canonical_digest(payload)
        with closing(self._connect(write=True)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._authorize_auxiliary_write(
                connection,
                authority=authority,
                lease_owner_id=lease_owner_id,
                lease_token=lease_token,
                project_id=project_id,
                entity_id=entity_id,
                operation="conflict_create",
                at=timestamp,
            )
            for event_id in members:
                row = connection.execute(
                    "SELECT project_id,entity_id FROM state_events WHERE event_id=?",
                    (event_id,),
                ).fetchone()
                if (
                    row is None
                    or row["project_id"] != project_id
                    or row["entity_id"] != entity_id
                ):
                    connection.rollback()
                    raise OperationalStateError("conflict_member_mismatch")
            connection.execute(
                "INSERT INTO state_conflicts VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    conflict_group_id,
                    project_id,
                    entity_id,
                    field_name,
                    "pending",
                    canonical_json(payload),
                    digest,
                    None,
                    timestamp,
                ),
            )
            self._write_projection(
                connection, entity_id, freshness="conflicted"
            )
            connection.commit()
        return {
            "conflict_group_id": conflict_group_id,
            "status": "pending",
            "conflict_digest": digest,
        }

    def record_verification(
        self,
        *,
        verification_id: str,
        entity_id: str,
        event_id: str | None,
        verifier_kind: str,
        result: str,
        evidence_digest: str,
        details: Mapping[str, Any],
        recorded_at: str,
        authority: AuthorityArtifact | None = None,
        lease_owner_id: str | None = None,
        lease_token: str | None = None,
    ) -> dict[str, Any]:
        validate_identity(verification_id, "invalid_verification_id")
        validate_sha256(evidence_digest, "invalid_verification_evidence_digest")
        if result not in {"passed", "failed", "unverifiable"}:
            raise OperationalStateError("invalid_verification_result")
        timestamp = normalize_timestamp(recorded_at)
        payload = {
            "verification_id": verification_id,
            "entity_id": entity_id,
            "event_id": event_id,
            "verifier_kind": verifier_kind,
            "result": result,
            "evidence_digest": evidence_digest,
            "details": details,
            "recorded_at": timestamp,
        }
        digest = canonical_digest(payload)
        with closing(self._connect(write=True)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            project_row = connection.execute(
                "SELECT project_id FROM state_projections WHERE entity_id=?",
                (entity_id,),
            ).fetchone()
            project_id = (
                project_row["project_id"]
                if project_row is not None
                else (
                    authority.target_project_id
                    if authority is not None
                    else "project:maintenance"
                )
            )
            self._authorize_auxiliary_write(
                connection,
                authority=authority,
                lease_owner_id=lease_owner_id,
                lease_token=lease_token,
                project_id=project_id,
                entity_id=entity_id,
                operation="verification_record",
                at=timestamp,
            )
            connection.execute(
                "INSERT INTO state_verifications VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    verification_id,
                    entity_id,
                    event_id,
                    verifier_kind,
                    result,
                    evidence_digest,
                    canonical_json(payload),
                    digest,
                    timestamp,
                ),
            )
            connection.commit()
        return {
            "verification_id": verification_id,
            "verification_digest": digest,
            "result": result,
        }

    def set_freshness(
        self,
        *,
        entity_id: str,
        freshness: str,
        authority: AuthorityArtifact | None = None,
        lease_owner_id: str | None = None,
        lease_token: str | None = None,
        recorded_at: str | None = None,
    ) -> dict[str, Any]:
        if freshness not in FRESHNESS_STATES:
            raise OperationalStateError("invalid_freshness")
        with closing(self._connect(write=True)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._projection_row(connection, entity_id)
            if row is None:
                connection.rollback()
                raise OperationalStateError("projection_not_found")
            timestamp = normalize_timestamp(recorded_at or utc_now())
            self._authorize_auxiliary_write(
                connection,
                authority=authority,
                lease_owner_id=lease_owner_id,
                lease_token=lease_token,
                project_id=row["project_id"],
                entity_id=entity_id,
                operation="freshness_update",
                at=timestamp,
            )
            projection = self._write_projection(
                connection, entity_id, freshness=freshness
            )
            connection.commit()
        return projection

    def backup_to(
        self,
        destination: str | os.PathLike[str],
    ) -> dict[str, Any]:
        target = _absolute(destination)
        if target.exists():
            raise OperationalStateError("backup_target_exists")
        try:
            authorization = authorize_alias_safe_test_database_target(
                target, require_exists=False
            )
        except KnowledgeSidecarPathError as exc:
            raise OperationalStateError("unsafe_backup_target") from exc
        descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_RDWR)
        os.close(descriptor)
        failed = True
        try:
            with closing(self._connect()) as source, closing(
                sqlite3.connect(target)
            ) as backup:
                source.backup(backup)
            restored = OperationalStateStore(target)
            report = restored.integrity_report()
            if (
                report.integrity_check != "ok"
                or report.foreign_key_violations != 0
                or report.schema_drift
            ):
                raise OperationalStateError("backup_validation_failed")
            failed = False
        finally:
            if failed and target.exists():
                target.unlink()
        return {
            "source": str(self.path),
            "destination": str(authorization.target),
            "size": target.stat().st_size,
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "integrity_check": report.integrity_check,
            "foreign_key_violations": report.foreign_key_violations,
            "schema_digest": report.schema_digest,
        }

    @staticmethod
    def restore_from_backup(
        source: str | os.PathLike[str],
        destination: str | os.PathLike[str],
    ) -> "OperationalStateStore":
        source_path = _absolute(source)
        if not source_path.is_file():
            raise OperationalStateError("backup_source_missing")
        source_store = OperationalStateStore(source_path)
        source_report = source_store.integrity_report()
        if (
            source_report.integrity_check != "ok"
            or source_report.foreign_key_violations != 0
            or source_report.schema_drift
        ):
            raise OperationalStateError("backup_source_invalid")
        target = _absolute(destination)
        try:
            authorize_alias_safe_test_database_target(
                target, require_exists=False
            )
        except KnowledgeSidecarPathError as exc:
            raise OperationalStateError("unsafe_restore_target") from exc
        descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_RDWR)
        os.close(descriptor)
        failed = True
        try:
            with closing(
                sqlite3.connect(
                    f"file:{source_path.as_posix()}?mode=ro", uri=True
                )
            ) as source_connection, closing(
                sqlite3.connect(target)
            ) as target_connection:
                source_connection.backup(target_connection)
            restored = OperationalStateStore(target)
            restored_report = restored.integrity_report()
            if restored_report != source_report:
                raise OperationalStateError("restore_validation_failed")
            failed = False
            return restored
        finally:
            if failed and target.exists():
                target.unlink()


def default_lease_interval(
    *,
    acquired_at: str | None = None,
    seconds: int = 120,
) -> tuple[str, str]:
    acquired = normalize_timestamp(acquired_at or utc_now())
    parsed = datetime.fromisoformat(acquired.replace("Z", "+00:00"))
    expires = (parsed + timedelta(seconds=seconds)).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")
    return acquired, expires


__all__ = [
    "DEFAULT_OPERATIONAL_STATE_PATH",
    "IntegrityReport",
    "Lease",
    "OperationalStateStore",
    "PRODUCTION_WRITE_ENV",
    "SCHEMA_MARKER",
    "WRITER_LEASE_NAME",
    "default_lease_interval",
    "initialize_operational_database",
    "is_production_operational_path",
    "schema_fingerprint",
]
