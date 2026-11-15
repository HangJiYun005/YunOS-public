from __future__ import annotations

from contextlib import closing
from pathlib import Path
import inspect
import sqlite3
import tempfile
import unittest

from memory.ingestion_core import (
    BatchState,
    IngestionCoreError,
    replay_batch_events,
    transition_batch,
)
from memory.ingestion_historical_schema import (
    HistoricalSchemaError,
    UNIFIED_V2_TABLE_DDL,
    UnifiedAuthorityRepository,
    build_manifest_subject,
    execute_historical_operation_handler,
)
from memory.ingestion_migrations import (
    HISTORICAL_MIGRATION_DDL,
    MigrationRehearsalError,
    apply_production_historical_migration,
    inspect_schema,
)
from tests.v83_historical_schema_fixtures import (
    CREATED_AT,
    EVIDENCE_POLICY_VERSION,
    TARGET_SNAPSHOT,
    create_v8301_database,
    historical_manifest,
    insert_historical_manifest,
    insert_source_manifest,
    migrate_v8302,
    subjects,
)


class UnifiedAuthoritySecurityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="ayunos-v83g0-security-")
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def database(self, name: str) -> Path:
        path = create_v8301_database(self.root / name)
        migrate_v8302(path)
        return path

    def test_production_path_is_hard_refused(self) -> None:
        with self.assertRaisesRegex(
            MigrationRehearsalError,
            "production_migration_not_authorized",
        ):
            apply_production_historical_migration()

    def test_manifest_subject_double_and_zero_ref_rejected_by_schema(self) -> None:
        database = self.database("subject-refs.db")
        source, historical, source_subject, _ = subjects()
        with closing(sqlite3.connect(database)) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            insert_source_manifest(conn, source)
            insert_historical_manifest(conn, historical)
            for source_id, historical_id in (
                (None, None),
                (source.manifest_id, historical.historical_manifest_id),
            ):
                with self.subTest(source=source_id is not None):
                    with self.assertRaises(sqlite3.IntegrityError):
                        conn.execute(
                            "INSERT INTO ingestion_manifest_subjects VALUES(?,?,?,?,?,?,?,?)",
                            (
                                source_subject.manifest_subject_id,
                                "source",
                                source_id,
                                historical_id,
                                source_subject.subject_digest,
                                None,
                                CREATED_AT,
                                source_subject.subject_schema_version,
                            ),
                        )

    def test_historical_placeholder_hash_rejected_by_schema(self) -> None:
        database = self.database("placeholder.db")
        manifest = historical_manifest()
        with closing(sqlite3.connect(database)) as conn:
            values = (
                manifest.historical_manifest_id,
                manifest.source_type,
                manifest.provenance_kind,
                manifest.archive_state,
                "0" * 64,
                None,
                None,
                None,
                manifest.evidence_policy_version,
                manifest.evidence_digest,
                manifest.reconstruction_basis_digest,
                manifest.validation_status,
                "{}",
                "{}",
                "[]",
                manifest.created_at,
                manifest.manifest_schema_version,
            )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO ingestion_historical_manifests VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    values,
                )

    def test_metadata_only_digest_rejected_by_schema(self) -> None:
        database = self.database("metadata.db")
        source, historical, _, historical_subject = subjects()
        with closing(sqlite3.connect(database)) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            insert_historical_manifest(conn, historical)
            repository = UnifiedAuthorityRepository(conn)
            repository.add_subject(historical_subject)
            batch = repository.create_batch(
                historical_subject,
                historical,
                batch_mode="historical_backfill",
                plan={},
                target_snapshot_fingerprint=TARGET_SNAPSHOT,
                timestamp=CREATED_AT,
                batch_id_factory=lambda: "batch_" + "b" * 32,
            )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO ingestion_historical_attachment_bindings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        "historical_attachment_binding_" + "a" * 64,
                        historical_subject.manifest_subject_id,
                        batch.batch_id,
                        "ym1_" + "a" * 52,
                        None,
                        "message-fixture",
                        "a" * 64,
                        "metadata_only",
                        "clear",
                        "legacy_unknown",
                        "b" * 64,
                        0,
                        CREATED_AT,
                        "v8.3-g0-attachment-binding-2",
                    ),
                )

    def test_resolved_attachment_requires_entity_digest_at_schema(self) -> None:
        sql = next(
            item for item in UNIFIED_V2_TABLE_DDL
            if "CREATE TABLE ingestion_historical_attachment_bindings" in item
        )
        self.assertIn(
            "attachment_state='resolved' AND entity_digest IS NOT NULL",
            sql,
        )

    def test_historical_batch_cannot_bypass_unified_approval(self) -> None:
        database = self.database("approval.db")
        _, historical, _, historical_subject = subjects()
        with closing(sqlite3.connect(database)) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            insert_historical_manifest(conn, historical)
            repository = UnifiedAuthorityRepository(conn)
            repository.add_subject(historical_subject)
            batch = repository.create_batch(
                historical_subject,
                historical,
                batch_mode="historical_backfill",
                plan={"x": 1},
                target_snapshot_fingerprint=TARGET_SNAPSHOT,
                timestamp=CREATED_AT,
                batch_id_factory=lambda: "batch_" + "b" * 32,
            )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO ingestion_executions_v2 VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        "execution_" + "a" * 64,
                        batch.batch_id,
                        "a" * 64,
                        "b" * 64,
                        "committing",
                        "c" * 64,
                        None,
                        None,
                        "not_required",
                        CREATED_AT,
                        CREATED_AT,
                        "v8.3-e-execution-bundle-1",
                    ),
                )

    def test_plan_digest_mismatch_rejected_by_unified_trigger(self) -> None:
        database = self.database("plan-digest.db")
        _, historical, _, subject = subjects()
        with closing(sqlite3.connect(database)) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            insert_historical_manifest(conn, historical)
            repository = UnifiedAuthorityRepository(conn)
            repository.add_subject(subject)
            batch = repository.create_batch(
                subject,
                historical,
                batch_mode="historical_backfill",
                plan={"x": 1},
                target_snapshot_fingerprint=TARGET_SNAPSHOT,
                timestamp=CREATED_AT,
                batch_id_factory=lambda: "batch_" + "b" * 32,
            )
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "approval_v2_exact_plan_mismatch",
            ):
                conn.execute(
                    "INSERT INTO ingestion_approvals_v2 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        "approval_" + "a" * 64,
                        batch.batch_id,
                        subject.manifest_subject_id,
                        "plan_" + "a" * 64,
                        "f" * 64,
                        "stage_plan_" + "a" * 64,
                        "a" * 64,
                        TARGET_SNAPSHOT,
                        "b" * 64,
                        "exact_plan",
                        "operator",
                        CREATED_AT,
                        "2026-07-30T00:00:00.000000Z",
                        "v8.3-e-approval-1",
                    ),
                )

    def test_event_sequence_gap_and_digest_tamper_fail(self) -> None:
        source, _, source_subject, _ = subjects()
        from memory.ingestion_historical_schema import create_unified_import_batch
        batch = create_unified_import_batch(
            source_subject,
            source,
            batch_mode="full",
            plan={},
            timestamp=CREATED_AT,
            batch_id_factory=lambda: "batch_" + "a" * 32,
        )
        updated, first = transition_batch(
            batch,
            BatchState.VALIDATING,
            actor_kind="operator",
            reason_code="validation_started",
            occurred_at="2026-07-29T00:01:00.000000Z",
        )
        _, second = transition_batch(
            updated,
            BatchState.PLANNED,
            actor_kind="operator",
            reason_code="planning_completed",
            occurred_at="2026-07-29T00:02:00.000000Z",
            previous_event_digest=first.event_digest,
        )
        with self.assertRaisesRegex(IngestionCoreError, "event_sequence_gap"):
            replay_batch_events(batch.batch_id, [second])
        object.__setattr__(first, "event_digest", "f" * 64)
        with self.assertRaisesRegex(IngestionCoreError, "event_digest_mismatch"):
            replay_batch_events(batch.batch_id, [first])

    def test_recovery_required_and_rollback_use_core_states(self) -> None:
        source, _, subject, _ = subjects()
        from memory.ingestion_historical_schema import create_unified_import_batch
        batch = create_unified_import_batch(
            subject,
            source,
            batch_mode="full",
            plan={},
            timestamp=CREATED_AT,
            batch_id_factory=lambda: "batch_" + "a" * 32,
        )
        sequence = (
            BatchState.VALIDATING,
            BatchState.PLANNED,
            BatchState.AWAITING_APPROVAL,
        )
        digest = ""
        for target in sequence:
            batch, event = transition_batch(
                batch,
                target,
                actor_kind="operator",
                reason_code="advance",
                occurred_at=CREATED_AT,
                previous_event_digest=digest,
            )
            digest = event.event_digest
        self.assertEqual("awaiting_approval", batch.current_state)
        self.assertIn(
            BatchState.RECOVERY_REQUIRED,
            {
                BatchState.RECOVERY_REQUIRED,
                BatchState.ROLLING_BACK,
                BatchState.ROLLED_BACK,
            },
        )

    def test_schema_contains_no_parallel_historical_authority(self) -> None:
        active = "\n".join(HISTORICAL_MIGRATION_DDL)
        for name in (
            "ingestion_historical_approvals",
            "ingestion_historical_executions",
            "ingestion_historical_verifications",
        ):
            self.assertNotIn(name, active)

    def test_historical_handler_has_no_second_state_machine(self) -> None:
        source = inspect.getsource(execute_historical_operation_handler)
        self.assertIn("execute_test_bundle", source)
        self.assertNotIn("BatchState", source)
        self.assertNotIn("LEGAL_TRANSITIONS", source)

    def test_active_schema_has_no_private_paths_or_legacy_mutation(self) -> None:
        active = "\n".join(HISTORICAL_MIGRATION_DDL).lower()
        self.assertNotIn("absolute_path", active)
        self.assertNotIn("archive_path", active)
        self.assertNotIn("alter table conversations", active)
        self.assertNotIn("alter table messages", active)


if __name__ == "__main__":
    unittest.main()
