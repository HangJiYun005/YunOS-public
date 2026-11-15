from __future__ import annotations

from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from memory.ingestion_core import canonical_json_bytes
from memory.production_real_import_authorization import (
    ProductionRealImportAuthorization,
    canonical_document_digest,
)
from memory.production_real_import_entry import (
    EXECUTION_STATE_SCHEMA_VERSION,
    SERVICE_RECORD_SCHEMA_VERSION,
    GitExecutionState,
    ProductionRealImportExecutionError,
    _tree_gate,
    _validate_not_replayed,
    validate_fixed_production_paths,
    validate_git_execution_state,
    validate_service_quiescence_record,
)
from tests.v83_production_real_import_fixtures import (
    build_test_authorization,
    build_test_package,
    utc_text,
)
from tools.run_authorized_real_import import build_parser


class ProductionRealImportEntryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="ayunos-real-import-entry-"
        )
        self.root = Path(self.temporary.name)
        self.database = self.root / "main.db"
        self.database.write_bytes(b"fixture")
        self.asset_root = self.root / "data" / "ingestion_assets"
        self.now = datetime.now(timezone.utc)
        self.package = build_test_package(now=self.now)
        self.authorization = ProductionRealImportAuthorization.from_mapping(
            build_test_authorization(self.package, now=self.now)
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_cli_requires_all_explicit_bindings(self) -> None:
        parser = build_parser()
        arguments = [
            "--approval-package", "approval.json",
            "--authorization-record", "authorization.json",
            "--import-plan-artifact", "plan.json",
            "--source-manifest-artifact", "manifest.json",
            "--archive", "source.zip",
            "--production-db", "main.db",
            "--production-asset-root", "assets",
            "--expected-package-digest", "a" * 64,
            "--expected-plan-digest", "b" * 64,
            "--expected-archive-sha256", "c" * 64,
            "--expected-database-sha256", "d" * 64,
            "--execution-root", "execution",
        ]
        for flag in arguments[::2]:
            with self.subTest(flag=flag), redirect_stderr(io.StringIO()):
                index = arguments.index(flag)
                candidate = arguments[:index] + arguments[index + 2:]
                with self.assertRaises(SystemExit):
                    parser.parse_args(candidate)

    def test_only_code_defined_database_and_asset_root_are_accepted(self) -> None:
        with (
            patch(
                "memory.production_real_import_entry.PRODUCTION_MAIN_DATABASE",
                self.database,
            ),
            patch(
                "memory.production_real_import_entry.PRODUCTION_ASSET_ROOT",
                self.asset_root,
            ),
        ):
            database, assets = validate_fixed_production_paths(
                self.database,
                self.asset_root,
            )
            self.assertEqual(self.database.resolve(), database)
            self.assertEqual(self.asset_root.resolve(strict=False), assets)
            with self.assertRaisesRegex(
                ProductionRealImportExecutionError,
                "production_database_path_mismatch",
            ):
                validate_fixed_production_paths(
                    self.root / "other.db",
                    self.asset_root,
                )
            with self.assertRaisesRegex(
                ProductionRealImportExecutionError,
                "production_asset_root_mismatch",
            ):
                validate_fixed_production_paths(
                    self.database,
                    self.root / "other-assets",
                )

    def test_git_commit_dirty_and_stage_gates_fail_closed(self) -> None:
        clean = GitExecutionState("a" * 40, True, True)
        validate_git_execution_state(clean, expected_commit="a" * 40)
        cases = (
            (GitExecutionState("b" * 40, True, True), "git_commit_mismatch"),
            (GitExecutionState("a" * 40, False, True), "tracked_worktree_dirty"),
            (GitExecutionState("a" * 40, True, False), "staging_not_empty"),
        )
        for state, reason in cases:
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(
                    ProductionRealImportExecutionError,
                    reason,
                ):
                    validate_git_execution_state(
                        state,
                        expected_commit="a" * 40,
                    )

    def test_service_record_is_exact_fresh_and_checked_live(self) -> None:
        record = {
            "record_schema_version": SERVICE_RECORD_SCHEMA_VERSION,
            "one_time_execution_id": self.authorization.one_time_execution_id,
            "code_commit": self.authorization.code_commit,
            "source_database_sha256": self.authorization.source_database_sha256,
            "services_quiesced": True,
            "listener_count_8765": 0,
            "active_writer_count": 0,
            "tailscale_service_running": True,
            "observed_at": utc_text(self.now - timedelta(minutes=1)),
            "expires_at": utc_text(self.now + timedelta(minutes=5)),
        }
        record["record_digest"] = canonical_document_digest(
            record,
            digest_field="record_digest",
        )
        (self.root / "services_quiesced.json").write_bytes(
            canonical_json_bytes(record)
        )
        with patch(
            "memory.production_real_import_entry._port_is_closed",
            return_value=True,
        ):
            validated = validate_service_quiescence_record(
                self.root,
                authorization=self.authorization,
                now=self.now,
            )
        self.assertTrue(validated["services_quiesced"])
        with patch(
            "memory.production_real_import_entry._port_is_closed",
            return_value=False,
        ):
            with self.assertRaisesRegex(
                ProductionRealImportExecutionError,
                "services_not_quiesced",
            ):
                validate_service_quiescence_record(
                    self.root,
                    authorization=self.authorization,
                    now=self.now,
                )

    def test_one_time_state_ledger_is_hash_chained_and_blocks_replay(self) -> None:
        ledger = self.root / "execution_state.jsonl"
        record = {
            "state_schema_version": EXECUTION_STATE_SCHEMA_VERSION,
            "one_time_execution_id": self.authorization.one_time_execution_id,
            "authorization_digest": self.authorization.authorization_digest,
            "approval_package_digest": self.authorization.approval_package_digest,
            "state": "consumed",
            "recorded_at": utc_text(self.now),
            "previous_state_digest": (
                "e3b0c44298fc1c149afbf4c8996fb924"
                "27ae41e4649b934ca495991b7852b855"
            ),
        }
        record["state_digest"] = canonical_document_digest(
            record,
            digest_field="state_digest",
        )
        ledger.write_text(json.dumps(record) + "\n", encoding="utf-8")
        with patch(
            "memory.production_real_import_entry.EXECUTION_STATE_LEDGER",
            ledger,
        ):
            with self.assertRaisesRegex(
                ProductionRealImportExecutionError,
                "one_time_execution_replayed",
            ):
                _validate_not_replayed(
                    self.authorization.one_time_execution_id,
                    self.root / "new-execution-root",
                )

    def test_asset_tree_fingerprint_is_content_addressed_and_stable(self) -> None:
        absent = _tree_gate(self.asset_root, project_root=self.root)
        self.assertEqual("absent", absent["root_state"])
        target = self.asset_root / "sha256" / "ab" / ("ab" * 32)
        target.parent.mkdir(parents=True)
        target.write_bytes(b"asset")
        first = _tree_gate(self.asset_root, project_root=self.root)
        second = _tree_gate(self.asset_root, project_root=self.root)
        self.assertEqual("populated", first["root_state"])
        self.assertEqual(first, second)
        self.assertEqual(1, first["inventory_count"])


if __name__ == "__main__":
    unittest.main()
