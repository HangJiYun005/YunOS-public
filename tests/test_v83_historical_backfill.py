from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from memory.ingestion_backfill import (
    EVIDENCE_CLASSIFICATIONS,
    EXECUTABLE_OPERATION_TYPES,
    FORBIDDEN_OPERATION_TYPES,
    HistoricalBackfillError,
    aggregate_evidence_source_digests,
    audit_historical_evidence,
    build_historical_backfill_plan,
    build_historical_manifest_and_subject,
    make_backfill_conflict,
    make_binding_candidate,
    make_historical_evidence,
)
from memory.ingestion_execution import IngestionExecutionError, make_execution_operation
from memory.ingestion_migrations import (
    HISTORICAL_MIGRATION_DIGEST,
    HISTORICAL_MIGRATION_VERSION,
)
from tests.v83_historical_backfill_fixtures import (
    CREATED_AT,
    EVIDENCE_SOURCES,
    create_historical_legacy_database,
)


class HistoricalEvidenceAndPlanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="ayunos-v83g-plan-")
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def audit(
        self,
        *,
        source_platform: str | None = None,
        report: bool = True,
        sources: dict[str, str] | None = None,
    ):
        database = create_historical_legacy_database(
            self.root / f"case-{len(list(self.root.iterdir()))}.db",
            source_platform=source_platform,
        )
        return audit_historical_evidence(
            database,
            evidence_source_digests=sources or EVIDENCE_SOURCES,
            legacy_report_available=report,
        )

    def test_classification_vocabulary_is_closed(self) -> None:
        self.assertEqual(
            {
                "native_authoritative",
                "deterministic_legacy",
                "probable_legacy",
                "legacy_unknown",
                "conflicting_evidence",
                "unsupported_source",
                "invalid_relationship",
            },
            set(EVIDENCE_CLASSIFICATIONS),
        )

    def test_native_records_are_authoritative(self) -> None:
        audit = self.audit(source_platform="chatgpt")
        self.assertEqual(3, audit.statistics["native_authoritative"])
        self.assertEqual(0, audit.statistics["deterministic_backfill_records"])

    def test_legacy_report_supports_deterministic_reconstruction(self) -> None:
        audit = self.audit()
        self.assertEqual(3, audit.statistics["deterministic_backfill_records"])
        self.assertEqual(0, audit.statistics["unknown_records"])

    def test_missing_report_remains_unknown(self) -> None:
        audit = self.audit(report=False)
        self.assertEqual(3, audit.statistics["legacy_unknown"])
        self.assertEqual(3, audit.statistics["unknown_records"])
        self.assertEqual(0, len(audit.binding_candidates))

    def test_evidence_source_order_does_not_change_digest(self) -> None:
        first = self.audit(sources={"fixture_a": "1" * 64, "fixture_b": "2" * 64})
        second = self.audit(sources={"fixture_b": "2" * 64, "fixture_a": "1" * 64})
        self.assertEqual(
            first.reconstruction_basis_digest,
            second.reconstruction_basis_digest,
        )

    def test_evidence_change_changes_plan(self) -> None:
        first = self.audit(sources={"fixture_a": "1" * 64})
        second = self.audit(sources={"fixture_a": "2" * 64})
        first_manifest, first_subject = build_historical_manifest_and_subject(
            first,
            created_at=CREATED_AT,
        )
        second_manifest, second_subject = build_historical_manifest_and_subject(
            second,
            created_at=CREATED_AT,
        )
        del first_manifest, second_manifest
        first_plan = build_historical_backfill_plan(
            first,
            first_subject,
            migration_version=HISTORICAL_MIGRATION_VERSION,
            migration_digest=HISTORICAL_MIGRATION_DIGEST,
        )
        second_plan = build_historical_backfill_plan(
            second,
            second_subject,
            migration_version=HISTORICAL_MIGRATION_VERSION,
            migration_digest=HISTORICAL_MIGRATION_DIGEST,
        )
        self.assertNotEqual(first_plan.plan_digest, second_plan.plan_digest)

    def test_plan_is_stable_for_same_snapshot_and_evidence(self) -> None:
        audit = self.audit()
        _, subject = build_historical_manifest_and_subject(
            audit,
            created_at=CREATED_AT,
        )
        first = build_historical_backfill_plan(
            audit,
            subject,
            migration_version=HISTORICAL_MIGRATION_VERSION,
            migration_digest=HISTORICAL_MIGRATION_DIGEST,
        )
        second = build_historical_backfill_plan(
            audit,
            subject,
            migration_version=HISTORICAL_MIGRATION_VERSION,
            migration_digest=HISTORICAL_MIGRATION_DIGEST,
        )
        self.assertEqual(first.plan_digest, second.plan_digest)

    def test_probable_candidate_is_deferred(self) -> None:
        evidence = make_historical_evidence(
            evidence_type="historical_record_reconstruction",
            target_record_kind="message",
            target_record_id_digest="3" * 64,
            source_type_candidate="chatgpt",
            source_record_id_digest="4" * 64,
            evidence_strength="probable_legacy",
            reason_code="multiple_source_candidates",
        )
        candidate = make_binding_candidate(
            target_record_kind="message",
            target_record_ref="fixture-message",
            proposed_source_type="chatgpt",
            proposed_source_record_id="fixture-source",
            provenance_kind="probable_legacy",
            evidence_ids=(evidence.evidence_id,),
            requires_approval=True,
            conflict_state="deferred",
        )
        self.assertEqual("deferred", candidate.conflict_state)
        self.assertTrue(candidate.requires_approval)

    def test_conflicting_evidence_is_blocked(self) -> None:
        evidence = make_historical_evidence(
            evidence_type="historical_record_reconstruction",
            target_record_kind="message",
            target_record_id_digest="5" * 64,
            source_type_candidate="chatgpt",
            source_record_id_digest="6" * 64,
            evidence_strength="conflicting_evidence",
            reason_code="source_identity_conflict",
        )
        candidate = make_binding_candidate(
            target_record_kind="message",
            target_record_ref="fixture-message",
            proposed_source_type="chatgpt",
            proposed_source_record_id="fixture-source",
            provenance_kind="conflicting_evidence",
            evidence_ids=(evidence.evidence_id,),
            requires_approval=True,
            conflict_state="blocked",
        )
        conflict = make_backfill_conflict(
            conflict_type="conflicting_evidence",
            severity="high",
            target_refs=("fixture-message",),
            candidate_refs=(candidate.candidate_id,),
            evidence_digests=(evidence.evidence_digest,),
            proposed_actions=("block_binding", "manual_review"),
            default_action="block_binding",
            requires_approval=True,
            reason_code="source_identity_conflict",
        )
        self.assertEqual("block_binding", conflict.default_action)
        self.assertTrue(conflict.requires_approval)

    def test_cross_source_identity_is_isolated(self) -> None:
        evidence = make_historical_evidence(
            evidence_type="historical_record_reconstruction",
            target_record_kind="message",
            target_record_id_digest="7" * 64,
            source_type_candidate="chatgpt",
            source_record_id_digest="8" * 64,
            evidence_strength="deterministic_legacy",
            reason_code="fixture_identity",
        )
        common = {
            "target_record_kind": "message",
            "target_record_ref": "fixture-message",
            "proposed_source_record_id": "same-source-id",
            "provenance_kind": "deterministic_legacy",
            "evidence_ids": (evidence.evidence_id,),
            "requires_approval": True,
            "conflict_state": "clear",
        }
        first = make_binding_candidate(proposed_source_type="chatgpt", **common)
        second = make_binding_candidate(proposed_source_type="claude", **common)
        self.assertNotEqual(
            first.source_record_id_digest,
            second.source_record_id_digest,
        )

    def test_plan_summary_contains_digests_not_private_record_ids(self) -> None:
        audit = self.audit()
        _, subject = build_historical_manifest_and_subject(
            audit,
            created_at=CREATED_AT,
        )
        plan = build_historical_backfill_plan(
            audit,
            subject,
            migration_version=HISTORICAL_MIGRATION_VERSION,
            migration_digest=HISTORICAL_MIGRATION_DIGEST,
        )
        serialized = str(plan.summary())
        self.assertNotIn("fixture-message", serialized)
        self.assertNotIn("fixture-conversation", serialized)

    def test_unknown_operation_type_fails_closed(self) -> None:
        with self.assertRaisesRegex(IngestionExecutionError, "unknown_operation_type"):
            make_execution_operation(
                plan_action_id="planaction_" + "1" * 64,
                operation_type="modify_old_table",
                payload={},
            )

    def test_operation_whitelist_and_forbidden_set_do_not_overlap(self) -> None:
        self.assertFalse(EXECUTABLE_OPERATION_TYPES & FORBIDDEN_OPERATION_TYPES)

    def test_invalid_evidence_digest_is_rejected(self) -> None:
        database = create_historical_legacy_database(self.root / "invalid.db")
        with self.assertRaisesRegex(
            HistoricalBackfillError,
            "invalid_evidence_source_digest",
        ):
            audit_historical_evidence(
                database,
                evidence_source_digests={"fixture": "not-a-digest"},
            )

    def test_evidence_file_aggregation_returns_only_digests(self) -> None:
        evidence_file = self.root / "evidence.txt"
        evidence_file.write_text("synthetic evidence", encoding="utf-8")
        result = aggregate_evidence_source_digests({"fixture": evidence_file})
        self.assertEqual(64, len(result["fixture"]))
        self.assertNotIn(str(evidence_file), str(dict(result)))


if __name__ == "__main__":
    unittest.main()
