"""Prototype-only SQLite repositories for YunOS v8.3-A ingestion metadata.

The path guard permits only the system temporary directory or this repository's
ignored ``output`` directory.  The ledger never opens or migrates the production
memory database.  All batch transitions are appended with their immutable event
inside one ``BEGIN IMMEDIATE`` transaction.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import uuid
from typing import Any

from memory.ingestion_core import (
    BatchEvent,
    BatchState,
    ImportBatch,
    IngestionCoreError,
    SourceManifest,
    canonical_json_bytes,
    replay_batch_events,
    transition_batch,
    utc_now,
    verify_batch_snapshot,
)


LEDGER_SCHEMA_VERSION = "v8.3-a-prototype-ledger-1"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = PROJECT_ROOT / "output"
TEMP_ROOT = Path(tempfile.gettempdir())
_LEASE_ID_RE = re.compile(r"[a-z][a-z0-9_.:-]{0,127}\Z")


class IngestionLedgerError(RuntimeError):
    """Sanitized prototype-ledger failure."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def validate_prototype_ledger_path(path: str | os.PathLike[str]) -> Path:
    target = Path(path).expanduser().resolve(strict=False)
    allowed = tuple(root.resolve(strict=False) for root in (TEMP_ROOT, OUTPUT_ROOT))
    if not any(_is_relative_to(target, root) for root in allowed):
        raise IngestionLedgerError("ledger_path_outside_prototype_roots")
    if target in allowed or target.suffix.lower() not in {".sqlite", ".db"}:
        raise IngestionLedgerError("invalid_ledger_path")
    production = (PROJECT_ROOT / "data" / "ayunos_chatgpt.db").resolve(strict=False)
    if target == production:
        raise IngestionLedgerError("production_database_forbidden")
    return target


def _decode_json(raw: str, reason_code: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        raise IngestionLedgerError(reason_code) from None
    if not isinstance(value, dict):
        raise IngestionLedgerError(reason_code)
    return value


@dataclass(frozen=True)
class WriterLease:
    lease_group_id: str
    batch_id: str
    manifest_id: str
    target_id: str
    owner: str
    created_at: str
    expires_at: str


@dataclass(frozen=True)
class LeaseDecision:
    status: str
    reason_code: str
    lease: WriterLease | None


class SQLiteIngestionLedger:
    """Cross-process prototype repository and single-writer lease store."""

    def __init__(self, path: str | os.PathLike[str], *, timeout_seconds: float = 5.0) -> None:
        self.path = validate_prototype_ledger_path(path)
        self.timeout_seconds = timeout_seconds

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=self.timeout_seconds,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={int(self.timeout_seconds * 1000)}")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS ingestion_ledger_schema(
                    schema_version TEXT PRIMARY KEY,
                    initialized_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS source_manifests(
                    manifest_id TEXT PRIMARY KEY,
                    archive_sha256 TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    manifest_digest TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS source_manifests_archive_idx
                    ON source_manifests(archive_sha256);
                CREATE TABLE IF NOT EXISTS import_batches(
                    batch_id TEXT PRIMARY KEY,
                    manifest_id TEXT NOT NULL,
                    current_state TEXT NOT NULL,
                    event_sequence INTEGER NOT NULL,
                    batch_json TEXT NOT NULL,
                    batch_digest TEXT NOT NULL,
                    FOREIGN KEY(manifest_id) REFERENCES source_manifests(manifest_id)
                );
                CREATE INDEX IF NOT EXISTS import_batches_manifest_idx
                    ON import_batches(manifest_id);
                CREATE TABLE IF NOT EXISTS batch_events(
                    event_id TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_json TEXT NOT NULL,
                    event_digest TEXT NOT NULL,
                    previous_event_digest TEXT NOT NULL,
                    UNIQUE(batch_id,sequence),
                    FOREIGN KEY(batch_id) REFERENCES import_batches(batch_id)
                );
                CREATE TRIGGER IF NOT EXISTS batch_events_no_update
                    BEFORE UPDATE ON batch_events
                    BEGIN SELECT RAISE(ABORT,'batch_events_append_only'); END;
                CREATE TRIGGER IF NOT EXISTS batch_events_no_delete
                    BEFORE DELETE ON batch_events
                    BEGIN SELECT RAISE(ABORT,'batch_events_append_only'); END;
                CREATE TABLE IF NOT EXISTS writer_leases(
                    scope_key TEXT PRIMARY KEY,
                    lease_group_id TEXT NOT NULL,
                    batch_id TEXT NOT NULL,
                    manifest_id TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    FOREIGN KEY(batch_id) REFERENCES import_batches(batch_id)
                );
                CREATE INDEX IF NOT EXISTS writer_leases_group_idx
                    ON writer_leases(lease_group_id);
                COMMIT;
                """
            )
            row = connection.execute(
                "SELECT schema_version FROM ingestion_ledger_schema"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO ingestion_ledger_schema VALUES(?,?)",
                    (LEDGER_SCHEMA_VERSION, utc_now()),
                )
            elif row["schema_version"] != LEDGER_SCHEMA_VERSION:
                raise IngestionLedgerError("ledger_schema_version_mismatch")

    @staticmethod
    def _canonical_text(value: Any) -> str:
        return canonical_json_bytes(value).decode("utf-8")

    @staticmethod
    def _digest(value: Any) -> str:
        import hashlib
        return hashlib.sha256(canonical_json_bytes(value)).hexdigest()

    def add_manifest(self, manifest: SourceManifest) -> None:
        payload = manifest.to_dict()
        encoded = self._canonical_text(payload)
        digest = self._digest(payload)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT manifest_digest FROM source_manifests WHERE manifest_id=?",
                (manifest.manifest_id,),
            ).fetchone()
            if row is not None:
                connection.rollback()
                if row["manifest_digest"] != digest:
                    raise IngestionLedgerError("manifest_id_collision")
                return
            connection.execute(
                "INSERT INTO source_manifests VALUES(?,?,?,?)",
                (manifest.manifest_id, manifest.archive_sha256, encoded, digest),
            )
            connection.commit()

    def get_manifest(self, manifest_id: str) -> SourceManifest | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT manifest_json,manifest_digest FROM source_manifests WHERE manifest_id=?",
                (manifest_id,),
            ).fetchone()
        if row is None:
            return None
        payload = _decode_json(row["manifest_json"], "manifest_record_corrupt")
        if self._digest(payload) != row["manifest_digest"]:
            raise IngestionLedgerError("manifest_record_tampered")
        try:
            return SourceManifest.from_dict(payload)
        except IngestionCoreError:
            raise IngestionLedgerError("manifest_record_corrupt") from None

    def find_manifests_by_archive_sha256(self, archive_sha256: str) -> list[SourceManifest]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT manifest_id FROM source_manifests WHERE archive_sha256=? ORDER BY manifest_id",
                (archive_sha256,),
            ).fetchall()
        return [
            manifest
            for row in rows
            if (manifest := self.get_manifest(row["manifest_id"])) is not None
        ]

    def add_batch(self, batch: ImportBatch) -> None:
        payload = batch.to_dict()
        encoded = self._canonical_text(payload)
        digest = self._digest(payload)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "INSERT INTO import_batches VALUES(?,?,?,?,?,?)",
                    (
                        batch.batch_id,
                        batch.manifest_id,
                        batch.current_state,
                        batch.event_sequence,
                        encoded,
                        digest,
                    ),
                )
            except sqlite3.IntegrityError:
                connection.rollback()
                raise IngestionLedgerError("batch_id_collision") from None
            connection.commit()

    def get_batch(self, batch_id: str) -> ImportBatch | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT batch_json,batch_digest,current_state,event_sequence "
                "FROM import_batches WHERE batch_id=?",
                (batch_id,),
            ).fetchone()
        if row is None:
            return None
        payload = _decode_json(row["batch_json"], "batch_record_corrupt")
        if self._digest(payload) != row["batch_digest"]:
            raise IngestionLedgerError("batch_record_tampered")
        try:
            batch = ImportBatch.from_dict(payload)
        except IngestionCoreError:
            raise IngestionLedgerError("batch_record_corrupt") from None
        if (
            batch.current_state != row["current_state"]
            or batch.event_sequence != row["event_sequence"]
        ):
            raise IngestionLedgerError("batch_snapshot_columns_mismatch")
        return batch

    def list_batches_for_manifest(self, manifest_id: str) -> list[ImportBatch]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT batch_id FROM import_batches WHERE manifest_id=? ORDER BY batch_id",
                (manifest_id,),
            ).fetchall()
        return [
            batch
            for row in rows
            if (batch := self.get_batch(row["batch_id"])) is not None
        ]

    def list_events(self, batch_id: str) -> list[BatchEvent]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT event_json,event_digest FROM batch_events "
                "WHERE batch_id=? ORDER BY sequence",
                (batch_id,),
            ).fetchall()
        events: list[BatchEvent] = []
        for row in rows:
            payload = _decode_json(row["event_json"], "event_record_corrupt")
            try:
                event = BatchEvent.from_dict(payload)
            except IngestionCoreError:
                raise IngestionLedgerError("event_record_tampered") from None
            if event.event_digest != row["event_digest"]:
                raise IngestionLedgerError("event_record_tampered")
            events.append(event)
        try:
            replay_batch_events(batch_id, events)
        except IngestionCoreError:
            raise IngestionLedgerError("event_replay_failed") from None
        return events

    def append_transition(self, batch: ImportBatch, event: BatchEvent) -> None:
        batch_payload = batch.to_dict()
        event_payload = event.to_dict()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT batch_json,batch_digest,current_state,event_sequence "
                "FROM import_batches WHERE batch_id=?",
                (batch.batch_id,),
            ).fetchone()
            if current is None:
                connection.rollback()
                raise IngestionLedgerError("batch_not_found")
            current_payload = _decode_json(current["batch_json"], "batch_record_corrupt")
            if self._digest(current_payload) != current["batch_digest"]:
                connection.rollback()
                raise IngestionLedgerError("batch_record_tampered")
            try:
                previous_batch = ImportBatch.from_dict(current_payload)
            except IngestionCoreError:
                connection.rollback()
                raise IngestionLedgerError("batch_record_corrupt") from None
            if (
                event.sequence != previous_batch.event_sequence + 1
                or event.previous_state != previous_batch.current_state
                or batch.event_sequence != event.sequence
                or batch.current_state != event.next_state
            ):
                connection.rollback()
                raise IngestionLedgerError("stale_or_inconsistent_transition")
            previous_event = connection.execute(
                "SELECT event_digest FROM batch_events WHERE batch_id=? "
                "ORDER BY sequence DESC LIMIT 1",
                (batch.batch_id,),
            ).fetchone()
            expected_previous = previous_event["event_digest"] if previous_event else ""
            if event.previous_event_digest != expected_previous:
                connection.rollback()
                raise IngestionLedgerError("event_chain_mismatch")
            try:
                verify_batch_snapshot(
                    batch,
                    [
                        BatchEvent.from_dict(
                            _decode_json(row["event_json"], "event_record_corrupt")
                        )
                        for row in connection.execute(
                            "SELECT event_json FROM batch_events WHERE batch_id=? ORDER BY sequence",
                            (batch.batch_id,),
                        ).fetchall()
                    ] + [event],
                )
            except IngestionCoreError:
                connection.rollback()
                raise IngestionLedgerError("event_replay_failed") from None
            try:
                connection.execute(
                    "INSERT INTO batch_events VALUES(?,?,?,?,?,?)",
                    (
                        event.event_id,
                        event.batch_id,
                        event.sequence,
                        self._canonical_text(event_payload),
                        event.event_digest,
                        event.previous_event_digest,
                    ),
                )
                cursor = connection.execute(
                    "UPDATE import_batches SET current_state=?,event_sequence=?,"
                    "batch_json=?,batch_digest=? WHERE batch_id=? AND event_sequence=?",
                    (
                        batch.current_state,
                        batch.event_sequence,
                        self._canonical_text(batch_payload),
                        self._digest(batch_payload),
                        batch.batch_id,
                        previous_batch.event_sequence,
                    ),
                )
            except sqlite3.IntegrityError:
                connection.rollback()
                raise IngestionLedgerError("event_append_conflict") from None
            if cursor.rowcount != 1:
                connection.rollback()
                raise IngestionLedgerError("event_append_conflict")
            connection.commit()

    def _lease_from_row(self, row: sqlite3.Row) -> WriterLease:
        return WriterLease(
            lease_group_id=row["lease_group_id"],
            batch_id=row["batch_id"],
            manifest_id=row["manifest_id"],
            target_id=row["target_id"],
            owner=row["owner"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
        )

    @staticmethod
    def _parse_utc(value: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            raise IngestionLedgerError("invalid_lease_timestamp") from None
        if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
            raise IngestionLedgerError("invalid_lease_timestamp")
        return parsed

    def acquire_writer_lease(
        self,
        *,
        batch_id: str,
        manifest_id: str,
        target_id: str,
        owner: str,
        created_at: str,
        expires_at: str,
    ) -> LeaseDecision:
        created = self._parse_utc(created_at)
        expires = self._parse_utc(expires_at)
        if expires <= created:
            raise IngestionLedgerError("invalid_lease_interval")
        if (
            not isinstance(owner, str)
            or _LEASE_ID_RE.fullmatch(owner) is None
            or not isinstance(target_id, str)
            or _LEASE_ID_RE.fullmatch(target_id) is None
        ):
            raise IngestionLedgerError("invalid_lease_identity")
        scopes = (f"manifest:{manifest_id}", f"target:{target_id}")
        group = "lease_" + uuid.uuid4().hex
        lease = WriterLease(
            group, batch_id, manifest_id, target_id, owner, created_at, expires_at,
        )
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM writer_leases WHERE scope_key IN (?,?) ORDER BY scope_key",
                scopes,
            ).fetchall()
            if rows:
                existing = self._lease_from_row(rows[0])
                connection.rollback()
                if self._parse_utc(existing.expires_at) <= created:
                    return LeaseDecision(
                        "recovery_required", "expired_lease_requires_recovery", existing,
                    )
                return LeaseDecision("blocked", "writer_lease_conflict", existing)
            try:
                connection.executemany(
                    "INSERT INTO writer_leases VALUES(?,?,?,?,?,?,?,?)",
                    [
                        (
                            scope,
                            group,
                            batch_id,
                            manifest_id,
                            target_id,
                            owner,
                            created_at,
                            expires_at,
                        )
                        for scope in scopes
                    ],
                )
            except sqlite3.IntegrityError:
                connection.rollback()
                return LeaseDecision("blocked", "writer_lease_conflict", None)
            connection.commit()
        return LeaseDecision("acquired", "writer_lease_acquired", lease)

    def inspect_writer_lease(
        self,
        *,
        batch_id: str,
        lease_group_id: str,
        owner: str,
        at: str,
    ) -> LeaseDecision:
        now = self._parse_utc(at)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM writer_leases WHERE lease_group_id=? ORDER BY scope_key",
                (lease_group_id,),
            ).fetchall()
        if len(rows) != 2:
            return LeaseDecision("recovery_required", "writer_lease_missing", None)
        lease = self._lease_from_row(rows[0])
        if lease.batch_id != batch_id or lease.owner != owner:
            return LeaseDecision("blocked", "writer_lease_owner_mismatch", lease)
        if self._parse_utc(lease.expires_at) <= now:
            return LeaseDecision(
                "recovery_required", "expired_lease_requires_recovery", lease,
            )
        return LeaseDecision("acquired", "writer_lease_active", lease)

    def release_writer_lease(
        self,
        *,
        batch_id: str,
        lease_group_id: str,
        owner: str,
    ) -> None:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT batch_id,owner FROM writer_leases WHERE lease_group_id=?",
                (lease_group_id,),
            ).fetchall()
            if len(rows) != 2 or any(
                row["batch_id"] != batch_id or row["owner"] != owner for row in rows
            ):
                connection.rollback()
                raise IngestionLedgerError("writer_lease_release_mismatch")
            connection.execute(
                "DELETE FROM writer_leases WHERE lease_group_id=?",
                (lease_group_id,),
            )
            connection.commit()

    def mark_lost_committing_lease(
        self,
        *,
        batch_id: str,
        actor_kind: str = "lease_monitor",
        occurred_at: str | None = None,
    ) -> ImportBatch:
        batch = self.get_batch(batch_id)
        if batch is None:
            raise IngestionLedgerError("batch_not_found")
        if batch.current_state != BatchState.COMMITTING.value:
            raise IngestionLedgerError("batch_not_committing")
        events = self.list_events(batch_id)
        previous_digest = events[-1].event_digest if events else ""
        try:
            updated, event = transition_batch(
                batch,
                BatchState.RECOVERY_REQUIRED,
                actor_kind=actor_kind,
                reason_code="writer_lease_lost",
                occurred_at=occurred_at or utc_now(),
                previous_event_digest=previous_digest,
            )
        except IngestionCoreError:
            raise IngestionLedgerError("recovery_transition_failed") from None
        self.append_transition(updated, event)
        return updated
