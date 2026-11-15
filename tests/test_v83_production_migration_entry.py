from __future__ import annotations

from contextlib import redirect_stderr
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from memory.ingestion_core import canonical_json_bytes
from memory.ingestion_migration_rehearsal import capture_sqlite_file_gates
from memory.ingestion_migrations import (
    MIGRATION_DIGEST,
    PRODUCTION_DATABASE,
    _lock_path,
    apply_migration_to_clone,
    build_migration_plan,
    inspect_schema,
)
from memory.production_migration_authorization import (
    ProductionMigrationAuthorization,
    canonical_document_digest,
    migration_plan_digest,
)
from memory.production_migration_entry import (
    BACKUP_RECORD_SCHEMA_VERSION,
    PRODUCTION_MAIN_DATABASE,
    SERVICE_RECORD_SCHEMA_VERSION,
    EXECUTION_STATE_SCHEMA_VERSION,
    GitExecutionState,
    ProductionMigrationExecutionError,
    _validate_backup_verification_record,
    _validate_not_replayed,
    _validate_service_quiescence_record,
    _wal_gate,
    validate_git_execution_state,
    validate_production_main_database_path,
    validate_production_marker_gate,
    validate_target_version,
)
from tests.v83_migration_fixtures import create_legacy_database
from tests.v83_production_migration_fixtures import (
    build_test_approval_package,
    build_test_authorization_document,
    hash_file,
    utc_text,
)
from tools.run_authorized_v8301_migration import build_parser


class ProductionMigrationEntryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="ayunos-v8301-entry-")
        self.root = Path(self.temp.name)
        self.database = create_legacy_database(self.root / "clone.db")
        self.plan = build_migration_plan(inspect_schema(self.database))
        self.database_sha = hash_file(self.database)
        self.now = datetime.now(timezone.utc)
        self.package = build_test_approval_package(
            plan=self.plan,
            source_database_sha256=self.database_sha,
            source_database_size=self.database.stat().st_size,
            now=self.now,
        )
        document = build_test_authorization_document(
            package=self.package,
            plan=self.plan,
            source_database_sha256=self.database_sha,
            now=self.now,
        )
        self.authorization = ProductionMigrationAuthorization.from_mapping(document)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_target_is_required_and_exact_8301(self) -> None:
        self.assertEqual(8301, validate_target_version(8301))
        for value in (None, "", "8301", "latest", "all", 8302, True):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    ProductionMigrationExecutionError,
                    "target_version_must_be_exact_8301",
                ):
                    validate_target_version(value)

    def test_cli_requires_every_explicit_binding_and_refuses_alias_targets(self) -> None:
        parser = build_parser()
        arguments = [
            "--target-version", "8301",
            "--approval-package", "approval.json",
            "--authorization-record", "authorization.json",
            "--production-db", "main.db",
            "--expected-db-sha256", "a" * 64,
            "--expected-current-schema", "b" * 64,
            "--expected-target-schema", "c" * 64,
            "--expected-migration-digest", MIGRATION_DIGEST,
            "--expected-plan-digest", "d" * 64,
            "--execution-root", "execution",
        ]
        for flag in arguments[::2]:
            with self.subTest(missing=flag), redirect_stderr(io.StringIO()):
                index = arguments.index(flag)
                candidate = arguments[:index] + arguments[index + 2:]
                with self.assertRaises(SystemExit):
                    parser.parse_args(candidate)
        for target in ("latest", "all", "8302", "", "not-an-integer"):
            with self.subTest(target=target), redirect_stderr(io.StringIO()):
                candidate = list(arguments)
                candidate[1] = target
                with self.assertRaises(SystemExit):
                    parser.parse_args(candidate)

    def test_only_fixed_main_path_is_accepted_without_file_drift(self) -> None:
        before = capture_sqlite_file_gates(PRODUCTION_DATABASE)
        validated = validate_production_main_database_path(PRODUCTION_DATABASE)
        after = capture_sqlite_file_gates(PRODUCTION_DATABASE)
        self.assertEqual(PRODUCTION_MAIN_DATABASE.resolve(strict=True), validated)
        self.assertEqual(before, after)
        rejected = (
            self.database,
            Path("E:/AyunOS/data/profile/approved_profile.db"),
            Path("E:/AyunOS/data/semantic/ayunos_semantic.db"),
            Path("E:/AyunOS/data/runtime/ayunos_runtime.db"),
            Path(r"\\server\share\ayunos_chatgpt.db"),
            Path(r"\\?\E:\AyunOS\data\ayunos_chatgpt.db"),
        )
        for path in rejected:
            with self.subTest(path=path):
                with self.assertRaises(ProductionMigrationExecutionError):
                    validate_production_main_database_path(path)

    def test_git_commit_dirty_and_staged_gates_fail_closed(self) -> None:
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
                    ProductionMigrationExecutionError,
                    reason,
                ):
                    validate_git_execution_state(
                        state,
                        expected_commit="a" * 40,
                    )

    def test_marker_already_applied_is_safe_and_v8302_is_hard_refused(self) -> None:
        apply_migration_to_clone(
            self.plan,
            self.database,
            rehearsal_id="rehearsal_v8301_marker_gate_001",
        )
        migrated = inspect_schema(self.database)
        outcome = validate_production_marker_gate(
            migrated,
            expected_target_schema=self.plan.expected_target_schema_fingerprint,
        )
        self.assertEqual("already_applied", outcome.status)
        simulated_v8302 = replace(migrated, migration_version=8302)
        with self.assertRaisesRegex(
            ProductionMigrationExecutionError,
            "v8302_marker_present",
        ) as captured:
            validate_production_marker_gate(
                simulated_v8302,
                expected_target_schema=self.plan.expected_target_schema_fingerprint,
            )
        self.assertTrue(captured.exception.recovery_required)

    def test_nonzero_wal_writer_lease_and_one_time_replay_are_blocked(self) -> None:
        wal = Path(str(self.database) + "-wal")
        wal.write_bytes(b"not-quiescent")
        size, _digest = _wal_gate(self.database)
        self.assertGreater(size, 0)

        lock = _lock_path(self.database.resolve())
        lock.write_text("held", encoding="utf-8")
        self.assertTrue(lock.exists())

        ledger = self.root / "state.jsonl"
        state = {
            "state_schema_version": EXECUTION_STATE_SCHEMA_VERSION,
            "one_time_execution_id": self.authorization.one_time_execution_id,
            "authorization_digest": self.authorization.authorization_digest,
            "approval_package_digest": self.authorization.approval_package_digest,
            "state": "consumed",
            "recorded_at": utc_text(self.now),
            "previous_state_digest": hashlib.sha256(b"").hexdigest(),
        }
        state["state_digest"] = canonical_document_digest(
            state,
            digest_field="state_digest",
        )
        ledger.write_text(json.dumps(state) + "\n", encoding="utf-8")
        with patch(
            "memory.production_migration_entry.EXECUTION_STATE_LEDGER",
            ledger,
        ):
            with self.assertRaisesRegex(
                ProductionMigrationExecutionError,
                "one_time_execution_replayed",
            ):
                _validate_not_replayed(
                    self.authorization.one_time_execution_id,
                    self.root,
                )

    def test_service_quiescence_requires_fresh_exact_zero_writer_record(self) -> None:
        record = {
            "record_schema_version": SERVICE_RECORD_SCHEMA_VERSION,
            "one_time_execution_id": self.authorization.one_time_execution_id,
            "code_commit": self.authorization.code_commit,
            "source_database_sha256": self.authorization.source_database_sha256,
            "services_quiesced": False,
            "active_writer_count": 1,
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
        with self.assertRaisesRegex(
            ProductionMigrationExecutionError,
            "services_not_quiesced",
        ):
            _validate_service_quiescence_record(
                self.root,
                authorization=self.authorization,
                now=self.now,
            )

    def test_backup_record_cannot_be_boolean_only_or_unverified(self) -> None:
        record = {
            "record_schema_version": BACKUP_RECORD_SCHEMA_VERSION,
            "one_time_execution_id": self.authorization.one_time_execution_id,
            "source_database_sha256": self.authorization.source_database_sha256,
            "source_schema_fingerprint": self.authorization.source_schema_fingerprint,
            "backup_manifest_relative_path": "backups/migrations/test/manifest.json",
            "backup_manifest_digest": self.package["backup"]["backup_manifest_digest"],
            "backup_set_digest": self.package["backup"]["backup_set_digest"],
            "database_backups": {},
            "restore_rehearsal_passed": False,
            "observed_at": utc_text(self.now - timedelta(minutes=1)),
            "expires_at": utc_text(self.now + timedelta(minutes=5)),
        }
        record["record_digest"] = canonical_document_digest(
            record,
            digest_field="record_digest",
        )
        (self.root / "backup_verification.json").write_bytes(
            canonical_json_bytes(record)
        )
        with self.assertRaisesRegex(
            ProductionMigrationExecutionError,
            "backup_verification_mismatch",
        ):
            _validate_backup_verification_record(
                self.root,
                authorization=self.authorization,
                package=self.package,
                now=self.now,
            )


if __name__ == "__main__":
    unittest.main()
