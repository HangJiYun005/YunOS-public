from __future__ import annotations

import inspect
import json
from pathlib import Path
import tempfile
import unittest

from memory.ingestion_migrations import (
    MigrationRehearsalError,
    apply_production_migration,
)
from memory.production_migration_entry import (
    REQUIRED_JOURNAL_EVENTS,
    ProductionMigrationExecutionError,
    append_execution_journal_event,
    apply_authorized_production_migration,
    assert_execution_protocol_static_guards,
)
from tools.run_authorized_v8301_migration import build_parser


class ProductionMigrationSecurityTest(unittest.TestCase):
    def test_old_production_refusal_remains_permanent(self) -> None:
        with self.assertRaisesRegex(
            MigrationRehearsalError,
            "production_migration_not_authorized",
        ):
            apply_production_migration()

    def test_cli_has_no_latest_all_or_implicit_target_mode(self) -> None:
        parser = build_parser()
        target = next(
            action
            for action in parser._actions
            if action.dest == "target_version"
        )
        self.assertTrue(target.required)
        self.assertIsNone(target.default)
        option_strings = {
            option
            for action in parser._actions
            for option in action.option_strings
        }
        self.assertNotIn("--latest", option_strings)
        self.assertNotIn("--all", option_strings)
        self.assertNotIn("--migrate-all", option_strings)

    def test_authorized_entry_has_no_boolean_or_environment_bypass(self) -> None:
        signature = inspect.signature(apply_authorized_production_migration)
        self.assertEqual(("request", "now"), tuple(signature.parameters))
        source = inspect.getsource(apply_authorized_production_migration)
        self.assertNotIn("os.environ", source)
        self.assertNotIn("production=True", source)
        self.assertNotIn("authorized=True", source)

    def test_v8301_ddl_exists_only_in_authoritative_migration_module(self) -> None:
        project = Path(__file__).resolve().parents[1]
        runtime_files = (
            project / "memory" / "production_migration_authorization.py",
            project / "memory" / "production_migration_entry.py",
            project / "memory" / "production_migration_clone.py",
            project / "tools" / "run_authorized_v8301_migration.py",
        )
        for path in runtime_files:
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("CREATE TABLE", source, path.name)
            self.assertNotIn("CREATE INDEX", source, path.name)
            self.assertNotIn("CREATE TRIGGER", source, path.name)
            self.assertNotIn("MIGRATION_DDL =", source, path.name)

    def test_runtime_has_no_network_or_stash_integration(self) -> None:
        project = Path(__file__).resolve().parents[1]
        runtime_files = (
            project / "memory" / "production_migration_authorization.py",
            project / "memory" / "production_migration_entry.py",
            project / "memory" / "production_migration_clone.py",
            project / "tools" / "run_authorized_v8301_migration.py",
        )
        source = "\n".join(path.read_text(encoding="utf-8") for path in runtime_files)
        for forbidden in (
            "requests",
            "urllib",
            "socket",
            "http.client",
            "git stash",
            "stash@{",
            "os.environ",
        ):
            self.assertNotIn(forbidden, source)

    def test_journal_is_append_only_chained_and_path_free(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="ayunos-v8301-private-user-name-"
        ) as directory:
            root = Path(directory)
            first = append_execution_journal_event(
                root,
                execution_id="execution_v8301_security_001",
                event="authorization_validated",
            )
            second = append_execution_journal_event(
                root,
                execution_id="execution_v8301_security_001",
                event="production_path_validated",
            )
            self.assertEqual(first["event_digest"], second["previous_event_digest"])
            raw = (root / "execution_journal.jsonl").read_text(encoding="utf-8")
            self.assertNotIn(str(root), raw)
            self.assertNotIn("tailnet", raw.lower())
            self.assertNotIn("cookie", raw.lower())
            self.assertNotIn("token", raw.lower())
            records = [json.loads(line) for line in raw.splitlines()]
            self.assertEqual(2, len(records))
            with self.assertRaisesRegex(
                ProductionMigrationExecutionError,
                "journal_event_invalid",
            ):
                append_execution_journal_event(
                    root,
                    execution_id="execution_v8301_security_001",
                    event="private_payload",
                )

    def test_required_journal_protocol_and_static_guards_are_complete(self) -> None:
        expected = {
            "authorization_validated",
            "production_path_validated",
            "source_fingerprint_validated",
            "services_quiesced_validated",
            "backup_validated",
            "lease_acquired",
            "transaction_started",
            "migration_applied",
            "marker_written",
            "transaction_committed",
            "verification_started",
            "verification_passed",
            "execution_completed",
            "execution_failed",
            "recovery_required",
        }
        self.assertEqual(expected, set(REQUIRED_JOURNAL_EVENTS))
        assert_execution_protocol_static_guards()


if __name__ == "__main__":
    unittest.main()
