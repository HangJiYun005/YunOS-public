from __future__ import annotations

from pathlib import Path
import re
import subprocess
import tempfile
import unittest

from memory.ingestion_backfill import HistoricalBackfillError
from memory.ingestion_backfill_execution import (
    execute_historical_backfill,
    validate_historical_clone_target,
)
from memory.ingestion_migrations import inspect_schema
from tests.v83_historical_backfill_fixtures import (
    CREATED_AT,
    EXPIRES_AT,
    build_historical_fixture,
)


class HistoricalBackfillSecurityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="ayunos-v83g-security-")
        self.root = Path(self.temp.name)
        self.project = Path(__file__).resolve().parents[1]

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_production_refusal_does_not_change_file_metadata(self) -> None:
        production = self.project / "data" / "ayunos_chatgpt.db"
        before = production.stat()
        with self.assertRaisesRegex(
            HistoricalBackfillError,
            "production_path_forbidden",
        ):
            validate_historical_clone_target(production)
        after = production.stat()
        self.assertEqual(
            (before.st_size, before.st_mtime_ns),
            (after.st_size, after.st_mtime_ns),
        )

    def test_non_temp_clone_is_refused(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="v83g-boundary-",
            dir=self.project / "tests",
        ) as local:
            candidate = Path(local) / "target.sqlite"
            candidate.touch()
            with self.assertRaisesRegex(
                HistoricalBackfillError,
                "clone_path_outside_allowed_boundary",
            ):
                validate_historical_clone_target(candidate)

    def test_output_evidence_directory_is_git_ignored(self) -> None:
        result = subprocess.run(
            [
                "git",
                "check-ignore",
                "output/v8.3-g-historical-backfill-compatibility/probe.json",
            ],
            cwd=self.project,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode)

    def test_inactive_historical_authority_tables_do_not_exist(self) -> None:
        fixture = build_historical_fixture(self.root)
        snapshot = inspect_schema(fixture.database)
        forbidden = {
            "ingestion_historical_batches",
            "ingestion_historical_batch_events",
            "ingestion_historical_conflicts",
            "ingestion_historical_decisions",
            "ingestion_historical_approvals",
            "ingestion_historical_executions",
            "ingestion_historical_execution_events",
            "ingestion_historical_verifications",
        }
        self.assertFalse(
            forbidden & set(snapshot.object_names)
        )

    def test_result_and_public_summaries_do_not_expose_private_ids(self) -> None:
        fixture = build_historical_fixture(self.root)
        result, _, _ = execute_historical_backfill(
            clone_path=fixture.database,
            manifest=fixture.manifest,
            subject=fixture.subject,
            plan=fixture.plan,
            approval_created_at=CREATED_AT,
            approval_expires_at=EXPIRES_AT,
        )
        public = (
            str(result.to_dict())
            + str(fixture.plan.summary())
            + str(fixture.audit.public_summary())
        )
        self.assertNotIn("fixture-message", public)
        self.assertNotIn("fixture-conversation", public)
        self.assertNotIn(str(self.root), public)

    def test_modules_contain_no_network_client_or_stash_access(self) -> None:
        combined = "\n".join(
            (self.project / relative).read_text(encoding="utf-8")
            for relative in (
                "memory/ingestion_backfill.py",
                "memory/ingestion_backfill_execution.py",
            )
        )
        self.assertIsNone(re.search(
            r"\b(requests|urllib|httpx|socket|websocket)\b",
            combined,
        ))
        self.assertNotIn("stash@", combined)
        self.assertNotIn("git stash", combined)

    def test_execution_sql_uses_parameters_for_private_values(self) -> None:
        source = (
            self.project / "memory" / "ingestion_backfill_execution.py"
        ).read_text(encoding="utf-8")
        self.assertNotRegex(source, r"VALUES\s*\(\s*\{")
        self.assertNotRegex(source, r"WHERE\s+[^\\n]*\{payload")
        self.assertGreaterEqual(source.count("VALUES(?,?,?"), 4)

    def test_security_outputs_contain_no_token_cookie_or_tailnet_fields(self) -> None:
        fixture = build_historical_fixture(self.root)
        summary = str(fixture.audit.public_summary()).lower()
        for forbidden in ("token", "cookie", "tailnet", "http://", "https://"):
            self.assertNotIn(forbidden, summary)


if __name__ == "__main__":
    unittest.main()
