from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from memory.ingestion_backfill_execution import execute_historical_backfill
from memory.ingestion_migration_rehearsal import (
    capture_legacy_compatibility,
    run_compatibility_suite,
)
from tests.v83_historical_backfill_fixtures import (
    CREATED_AT,
    EXPIRES_AT,
    build_historical_fixture,
)


class HistoricalBackfillCompatibilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="ayunos-v83g-compat-")
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_case(self):
        fixture = build_historical_fixture(self.root)
        before = capture_legacy_compatibility(fixture.database)
        result, _, _ = execute_historical_backfill(
            clone_path=fixture.database,
            manifest=fixture.manifest,
            subject=fixture.subject,
            plan=fixture.plan,
            approval_created_at=CREATED_AT,
            approval_expires_at=EXPIRES_AT,
        )
        after = capture_legacy_compatibility(fixture.database)
        return fixture, result, before, after

    def test_all_legacy_contract_checks_pass(self) -> None:
        _, result, before, after = self.run_case()
        compatibility = run_compatibility_suite(
            before,
            after,
            require_empty_ingestion_tables=False,
        )
        self.assertEqual("completed", result.final_state)
        self.assertTrue(compatibility["passed"])
        self.assertTrue(all(compatibility["checks"].values()))

    def test_counts_and_id_sets_are_unchanged(self) -> None:
        _, _, before, after = self.run_case()
        self.assertEqual(before["row_counts"], after["row_counts"])
        self.assertEqual(before["id_set_digests"], after["id_set_digests"])

    def test_source_and_attachment_json_are_unchanged(self) -> None:
        _, _, before, after = self.run_case()
        self.assertEqual(
            before["source_values_digest"],
            after["source_values_digest"],
        )
        self.assertEqual(
            before["attachment_json_digest"],
            after["attachment_json_digest"],
        )

    def test_fts_desktop_and_reader_contracts_are_unchanged(self) -> None:
        _, _, before, after = self.run_case()
        for key in (
            "fts_contract_digest",
            "fts_row_count",
            "desktop_query_digest",
            "message_context_digest",
            "conversation_messages_digest",
        ):
            self.assertEqual(before[key], after[key], key)

    def test_no_asset_entity_is_fabricated(self) -> None:
        fixture, _, _, _ = self.run_case()
        with closing(sqlite3.connect(fixture.database)) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM ingestion_asset_entities"
            ).fetchone()[0]
            binding = connection.execute(
                "SELECT entity_digest,attachment_state "
                "FROM ingestion_historical_attachment_bindings"
            ).fetchone()
        self.assertEqual(0, count)
        self.assertIsNone(binding[0])
        self.assertEqual("asset_record_missing", binding[1])

    def test_media_identity_and_occurrence_are_stable(self) -> None:
        fixture, _, _, _ = self.run_case()
        expected = fixture.audit.attachment_binding_candidates[0]
        with closing(sqlite3.connect(fixture.database)) as connection:
            actual = connection.execute(
                "SELECT logical_media_id,occurrence_index,entity_digest "
                "FROM ingestion_historical_attachment_bindings"
            ).fetchone()
        self.assertEqual(expected.logical_media_id, actual[0])
        self.assertTrue(actual[0].startswith("ym1_"))
        self.assertEqual(expected.occurrence_index, actual[1])
        self.assertIsNone(actual[2])

    def test_integrity_and_foreign_keys_remain_clean(self) -> None:
        fixture, _, _, _ = self.run_case()
        with closing(sqlite3.connect(fixture.database)) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_keys = connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
        self.assertEqual("ok", integrity)
        self.assertEqual([], foreign_keys)


if __name__ == "__main__":
    unittest.main()
