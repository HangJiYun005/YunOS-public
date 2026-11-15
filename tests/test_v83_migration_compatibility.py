from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from memory.ingestion_migration_rehearsal import (
    capture_legacy_compatibility,
    persist_synthetic_integration,
    run_compatibility_suite,
)
from memory.ingestion_migrations import inspect_schema
from tests.v83_migration_fixtures import create_legacy_database, migrate_database


class MigrationCompatibilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="ayunos-v83f-compat-")
        self.database = create_legacy_database(Path(self.temp.name) / "legacy.db")
        self.before = capture_legacy_compatibility(self.database)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_legacy_counts_ids_fields_and_queries_are_exact_after_migration(self) -> None:
        migrate_database(self.database)
        after = capture_legacy_compatibility(self.database)
        summary = run_compatibility_suite(self.before, after)
        self.assertTrue(summary["passed"], summary["checks"])
        self.assertTrue(all(summary["checks"].values()))

    def test_source_source_id_and_attachment_json_contract_is_unchanged(self) -> None:
        migrate_database(self.database)
        after = capture_legacy_compatibility(self.database)
        self.assertEqual(
            {
                "conversations.source_platform": True,
                "conversations.source_id": False,
                "messages.source_platform": True,
                "messages.source_id": False,
            },
            after["source_column_presence"],
        )
        self.assertEqual(
            self.before["source_values_digest"], after["source_values_digest"],
        )
        self.assertEqual(
            self.before["attachment_json_digest"], after["attachment_json_digest"],
        )

    def test_fts_message_context_and_conversation_reader_contracts_match(self) -> None:
        migrate_database(self.database)
        after = capture_legacy_compatibility(self.database)
        for field in (
            "fts_contract_digest",
            "fts_row_count",
            "message_context_digest",
            "conversation_messages_digest",
        ):
            self.assertEqual(self.before[field], after[field], field)

    def test_readonly_open_and_integrity_pass_after_migration(self) -> None:
        migrate_database(self.database)
        after = capture_legacy_compatibility(self.database)
        self.assertTrue(after["readonly_open_passed"])
        self.assertEqual("ok", after["integrity_check"])
        self.assertEqual(0, after["foreign_key_violation_count"])

    def test_synthetic_new_rows_do_not_change_legacy_contract(self) -> None:
        migrate_database(self.database)
        result = persist_synthetic_integration(self.database)
        after = capture_legacy_compatibility(self.database)
        summary = run_compatibility_suite(
            self.before, after, require_empty_ingestion_tables=False,
        )
        self.assertTrue(result["passed"])
        self.assertTrue(summary["passed"])
        self.assertEqual(
            inspect_schema(self.database).row_counts,
            {"assets": 0, "conversations": 1, "messages": 2},
        )


if __name__ == "__main__":
    unittest.main()
