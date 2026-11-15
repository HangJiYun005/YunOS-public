from __future__ import annotations

from dataclasses import replace
import unittest

from memory.ingestion_approval import validate_approval_artifact
from memory.ingestion_core import (
    BatchState,
    IngestionCoreError,
    build_source_manifest,
    create_import_batch,
    replay_batch_events,
    transition_batch,
)
from memory.ingestion_execution import (
    acquire_writer_lease,
    append_execution_journal_event,
    execute_test_bundle,
)
from memory.ingestion_historical_schema import (
    HistoricalSchemaError,
    ManifestSubject,
    acquire_unified_writer_lease,
    append_unified_execution_journal_event,
    build_historical_attachment_binding,
    build_historical_source_binding,
    build_historical_source_manifest,
    build_manifest_subject,
    create_unified_import_batch,
    execute_historical_operation_handler,
    validate_unified_approval,
    verify_unified_execution,
)
from memory.ingestion_verification import verify_test_execution
from tests.v83_historical_schema_fixtures import (
    CREATED_AT,
    ENTITY_DIGEST,
    EVIDENCE_DIGEST,
    EVIDENCE_POLICY_VERSION,
    LOGICAL_MEDIA_ID,
    RECONSTRUCTION_BASIS_DIGEST,
    SOURCE_RECORD_DIGEST,
    historical_manifest,
    ordinary_manifest,
    subjects,
)


class HistoricalManifestAndUnifiedAuthorityTest(unittest.TestCase):
    def test_ordinary_source_manifest_remains_strict(self) -> None:
        with self.assertRaisesRegex(IngestionCoreError, "invalid_archive_digest"):
            build_source_manifest(
                source_type="chatgpt",
                source_version="export_v1",
                adapter_name="adapter",
                adapter_version="v1",
                archive_sha256="",
                archive_size=1,
                archive_kind="zip",
                discovered_at=CREATED_AT,
                scanned_at=CREATED_AT,
            )

    def test_historical_manifest_uses_honest_nullable_archive(self) -> None:
        manifest = historical_manifest()
        self.assertEqual("unavailable", manifest.archive_state)
        self.assertIsNone(manifest.archive_sha256)
        self.assertIsNone(manifest.archive_size)
        self.assertIsNone(manifest.adapter_name)
        self.assertEqual(RECONSTRUCTION_BASIS_DIGEST,
                         manifest.reconstruction_basis_digest)

    def test_historical_manifest_rejects_placeholder_or_real_hash(self) -> None:
        for digest in ("0" * 64, "a" * 64):
            with self.subTest(digest=digest[:1]):
                with self.assertRaisesRegex(
                    HistoricalSchemaError,
                    "unavailable_archive_must_not_have_archive_facts",
                ):
                    build_historical_source_manifest(
                        source_type="legacy",
                        provenance_kind="legacy_unknown",
                        archive_sha256=digest,  # type: ignore[arg-type]
                        archive_size=None,
                        adapter_name=None,
                        adapter_version=None,
                        evidence_policy_version=EVIDENCE_POLICY_VERSION,
                        evidence_digest=EVIDENCE_DIGEST,
                        reconstruction_basis_digest=RECONSTRUCTION_BASIS_DIGEST,
                        validation_status="evidence_reconstructed",
                        record_counts={},
                        asset_counts={},
                        warnings=(),
                        created_at=CREATED_AT,
                    )

    def test_historical_manifest_requires_reconstruction_basis(self) -> None:
        with self.assertRaisesRegex(
            HistoricalSchemaError,
            "invalid_reconstruction_basis_digest",
        ):
            build_historical_source_manifest(
                source_type="legacy",
                provenance_kind="legacy_unknown",
                adapter_name=None,
                adapter_version=None,
                evidence_policy_version=EVIDENCE_POLICY_VERSION,
                evidence_digest=EVIDENCE_DIGEST,
                reconstruction_basis_digest="0" * 64,
                validation_status="evidence_reconstructed",
                record_counts={},
                asset_counts={},
                warnings=(),
                created_at=CREATED_AT,
            )

    def test_manifest_subject_accepts_each_kind(self) -> None:
        source, historical, source_subject, historical_subject = subjects()
        self.assertEqual(source.manifest_id, source_subject.source_manifest_id)
        self.assertEqual(
            historical.historical_manifest_id,
            historical_subject.historical_manifest_id,
        )
        self.assertNotEqual(
            source_subject.manifest_subject_id,
            historical_subject.manifest_subject_id,
        )

    def test_manifest_subject_rejects_zero_and_double_reference(self) -> None:
        source, historical, _, _ = subjects()
        for values in (
            {},
            {"source_manifest": source, "historical_manifest": historical},
        ):
            with self.subTest(values=tuple(values)):
                with self.assertRaisesRegex(
                    HistoricalSchemaError,
                    "manifest_subject_requires_exactly_one_ref",
                ):
                    build_manifest_subject(created_at=CREATED_AT, **values)

    def test_manifest_subject_digest_is_deterministic(self) -> None:
        source = ordinary_manifest()
        first = build_manifest_subject(
            source_manifest=source,
            created_at=CREATED_AT,
        )
        second = build_manifest_subject(
            source_manifest=source,
            created_at="2026-07-29T00:01:00.000000Z",
        )
        self.assertEqual(first.subject_digest, second.subject_digest)
        self.assertEqual(first.manifest_subject_id, second.manifest_subject_id)

    def test_subject_constructor_rejects_double_reference(self) -> None:
        source, historical, source_subject, _ = subjects()
        with self.assertRaisesRegex(
            HistoricalSchemaError,
            "manifest_subject_requires_exactly_one_ref",
        ):
            ManifestSubject(
                manifest_subject_id=source_subject.manifest_subject_id,
                manifest_kind="source",
                source_manifest_id=source.manifest_id,
                historical_manifest_id=historical.historical_manifest_id,
                subject_digest=source_subject.subject_digest,
                evidence_policy_version=None,
                created_at=CREATED_AT,
            )

    def test_full_and_historical_use_same_batch_constructor_and_transition(self) -> None:
        source, historical, source_subject, historical_subject = subjects()
        full = create_unified_import_batch(
            source_subject,
            source,
            batch_mode="full",
            plan={"operations": []},
            timestamp=CREATED_AT,
            batch_id_factory=lambda: "batch_" + "a" * 32,
        )
        backfill = create_unified_import_batch(
            historical_subject,
            historical,
            batch_mode="historical_backfill",
            plan={"operations": []},
            timestamp=CREATED_AT,
            batch_id_factory=lambda: "batch_" + "b" * 32,
        )
        self.assertIs(type(full), type(backfill))
        for batch in (full, backfill):
            updated, event = transition_batch(
                batch,
                BatchState.VALIDATING,
                actor_kind="test_operator",
                reason_code="validation_started",
                occurred_at="2026-07-29T00:01:00.000000Z",
            )
            replay = replay_batch_events(batch.batch_id, [event])
            self.assertEqual(updated.current_state, replay.current_state)
            self.assertEqual(1, replay.event_sequence)

    def test_historical_adapter_null_does_not_weaken_ordinary_manifest(self) -> None:
        historical = historical_manifest()
        self.assertIsNone(historical.adapter_name)
        source = ordinary_manifest()
        with self.assertRaisesRegex(IngestionCoreError, "invalid_adapter_name"):
            create_import_batch(
                replace(source, adapter_name=None),  # type: ignore[arg-type]
                batch_mode="full",
                plan={},
            )

    def test_authority_aliases_are_exact_v83e_functions(self) -> None:
        self.assertIs(validate_unified_approval, validate_approval_artifact)
        self.assertIs(
            append_unified_execution_journal_event,
            append_execution_journal_event,
        )
        self.assertIs(acquire_unified_writer_lease, acquire_writer_lease)
        self.assertIs(verify_unified_execution, verify_test_execution)

    def test_historical_handler_calls_unified_executor(self) -> None:
        self.assertIs(execute_historical_operation_handler.__globals__[
            "execute_test_bundle"
        ], execute_test_bundle)

    def test_legacy_unknown_binding_has_no_fabricated_source_id(self) -> None:
        _, _, _, historical_subject = subjects()
        binding = build_historical_source_binding(
            manifest_subject_id=historical_subject.manifest_subject_id,
            unified_batch_id="batch_" + "b" * 32,
            target_record_kind="conversation",
            target_record_id="conversation-fixture",
            proposed_source_type="unknown",
            source_record_id_digest=None,
            provenance_kind="legacy_unknown",
            evidence_digest=EVIDENCE_DIGEST,
            binding_state="recorded_unknown",
            created_at=CREATED_AT,
        )
        self.assertIsNone(binding.source_record_id_digest)

    def test_probable_and_conflicting_bindings_cannot_autocomplete(self) -> None:
        _, _, _, subject = subjects()
        for provenance, state, reason in (
            ("probable_legacy", "verified", "probable_binding_cannot_be_automatic"),
            ("conflicting_evidence", "approved", "conflicting_binding_must_be_blocked"),
        ):
            with self.subTest(provenance=provenance):
                with self.assertRaisesRegex(HistoricalSchemaError, reason):
                    build_historical_source_binding(
                        manifest_subject_id=subject.manifest_subject_id,
                        unified_batch_id="batch_" + "b" * 32,
                        target_record_kind="message",
                        target_record_id="message-fixture",
                        proposed_source_type="legacy",
                        source_record_id_digest=SOURCE_RECORD_DIGEST,
                        provenance_kind=provenance,
                        evidence_digest=EVIDENCE_DIGEST,
                        binding_state=state,
                        created_at=CREATED_AT,
                    )

    def test_metadata_only_entity_is_nullable(self) -> None:
        _, _, _, subject = subjects()
        binding = build_historical_attachment_binding(
            manifest_subject_id=subject.manifest_subject_id,
            unified_batch_id="batch_" + "b" * 32,
            logical_media_id=LOGICAL_MEDIA_ID,
            source_attachment_id_digest=None,
            message_id="message-fixture",
            entity_digest=None,
            attachment_state="metadata_only",
            size_conflict_state="clear",
            provenance_kind="legacy_unknown",
            evidence_digest=EVIDENCE_DIGEST,
            occurrence_index=0,
            created_at=CREATED_AT,
        )
        self.assertIsNone(binding.entity_digest)

    def test_resolved_entity_requires_real_digest(self) -> None:
        _, _, _, subject = subjects()
        common = dict(
            manifest_subject_id=subject.manifest_subject_id,
            unified_batch_id="batch_" + "b" * 32,
            logical_media_id=LOGICAL_MEDIA_ID,
            source_attachment_id_digest=None,
            message_id="message-fixture",
            attachment_state="resolved",
            size_conflict_state="clear",
            provenance_kind="deterministic_legacy",
            evidence_digest=EVIDENCE_DIGEST,
            occurrence_index=0,
            created_at=CREATED_AT,
        )
        with self.assertRaisesRegex(
            HistoricalSchemaError,
            "resolved_entity_requires_digest",
        ):
            build_historical_attachment_binding(entity_digest=None, **common)
        resolved = build_historical_attachment_binding(
            entity_digest=ENTITY_DIGEST,
            **common,
        )
        self.assertEqual(ENTITY_DIGEST, resolved.entity_digest)

    def test_size_conflict_never_has_entity(self) -> None:
        _, _, _, subject = subjects()
        with self.assertRaisesRegex(
            HistoricalSchemaError,
            "unresolved_size_conflict_must_not_have_entity",
        ):
            build_historical_attachment_binding(
                manifest_subject_id=subject.manifest_subject_id,
                unified_batch_id="batch_" + "b" * 32,
                logical_media_id=LOGICAL_MEDIA_ID,
                source_attachment_id_digest=None,
                message_id="message-fixture",
                entity_digest=ENTITY_DIGEST,
                attachment_state="resolved",
                size_conflict_state="size_conflict_unresolved",
                provenance_kind="deterministic_legacy",
                evidence_digest=EVIDENCE_DIGEST,
                occurrence_index=0,
                created_at=CREATED_AT,
            )


if __name__ == "__main__":
    unittest.main()
