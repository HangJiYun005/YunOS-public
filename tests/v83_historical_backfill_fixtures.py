from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
import json
import sqlite3

from memory.ingestion_backfill import (
    HistoricalAudit,
    HistoricalBackfillPlan,
    audit_historical_evidence,
    build_historical_backfill_plan,
    build_historical_manifest_and_subject,
)
from memory.ingestion_historical_schema import (
    HistoricalSourceManifest,
    ManifestSubject,
)
from memory.ingestion_migrations import (
    HISTORICAL_MIGRATION_DIGEST,
    HISTORICAL_MIGRATION_VERSION,
    apply_historical_migration_to_clone,
    apply_migration_to_clone,
    build_historical_migration_plan,
    build_migration_plan,
    inspect_schema,
    inspect_v8301_schema,
)
from tests.v83_migration_fixtures import create_legacy_database


CREATED_AT = "2026-07-29T00:00:00.000000Z"
EXPIRES_AT = "2026-07-30T00:00:00.000000Z"
EVIDENCE_SOURCES = {
    "fixture_import_report": "1" * 64,
    "fixture_snapshot": "2" * 64,
}


@dataclass(frozen=True)
class HistoricalFixture:
    database: Path
    audit: HistoricalAudit
    manifest: HistoricalSourceManifest
    subject: ManifestSubject
    plan: HistoricalBackfillPlan


def create_historical_legacy_database(
    path: Path,
    *,
    source_platform: str | None = None,
) -> Path:
    create_legacy_database(path)
    attachment = json.dumps(
        {
            "attachments": [
                {
                    "id": "fixture-attachment-1",
                    "name": "fixture.png",
                    "mime_type": "image/png",
                    "size": 7,
                }
            ]
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            "UPDATE conversations SET source_platform=?",
            (source_platform,),
        )
        connection.execute(
            "UPDATE messages SET source_platform=?",
            (source_platform,),
        )
        connection.execute(
            "UPDATE messages SET metadata_json=? WHERE ordinal=1",
            (attachment,),
        )
        connection.commit()
    return path


def migrate_historical_fixture(path: Path) -> None:
    v8301 = build_migration_plan(inspect_schema(path))
    apply_migration_to_clone(
        v8301,
        path,
        rehearsal_id="rehearsal_v83g_fixture_8301",
    )
    v8302 = build_historical_migration_plan(inspect_v8301_schema(path))
    apply_historical_migration_to_clone(
        v8302,
        path,
        rehearsal_id="rehearsal_v83g_fixture_8302",
    )


def build_historical_fixture(
    root: Path,
    *,
    source_platform: str | None = None,
    legacy_report_available: bool = True,
) -> HistoricalFixture:
    database = create_historical_legacy_database(
        root / "target.sqlite",
        source_platform=source_platform,
    )
    audit = audit_historical_evidence(
        database,
        evidence_source_digests=EVIDENCE_SOURCES,
        legacy_report_available=legacy_report_available,
    )
    manifest, subject = build_historical_manifest_and_subject(
        audit,
        created_at=CREATED_AT,
    )
    plan = build_historical_backfill_plan(
        audit,
        subject,
        migration_version=HISTORICAL_MIGRATION_VERSION,
        migration_digest=HISTORICAL_MIGRATION_DIGEST,
    )
    migrate_historical_fixture(database)
    return HistoricalFixture(
        database=database,
        audit=audit,
        manifest=manifest,
        subject=subject,
        plan=plan,
    )
