from __future__ import annotations

import inspect
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from memory import cognitive_production_migration as production_migration
from memory.cognitive_production_migration import (
    COGNITIVE_PRODUCTION_ROLLBACK_POLICY,
    CognitiveProductionMigrationError,
    _append_ledger_state,
    _apply_fixed_cognitive_ddl,
    _execute_bound_cognitive_migration,
    _execution_lock,
    _read_ledger,
    _reconcile_bound_cognitive_migration,
    cognitive_production_recovery_policy,
)
from memory.cognitive_state_migration import (
    _COGNITIVE_DDL,
    verify_cognitive_state_schema,
)
from memory.operational_state_store import schema_fingerprint
from tests.v10_f_production_migration_fixtures import (
    NOW,
    T3,
    build_bound_fixture,
    clean_code_state,
    clone_execution_kwargs,
    clone_package_verifier,
)


class V10FMigrationRecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="ayunos-v10f-recovery-")
        self.root = Path(self.temp.name)
        fixture = build_bound_fixture(self.root)
        self.database = fixture["database"]
        self.paths = fixture["paths"]
        self.authority = fixture["authority"]
        self.packaged_database = fixture["packaged_database"]
        self.package = fixture["package"]
        self.plan = fixture["plan"]

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_with_fault(self, checkpoint_name: str):
        def checkpoint(name: str) -> None:
            if name == checkpoint_name:
                raise CognitiveProductionMigrationError(
                    "injected_migration_failure"
                )

        with patch(
            "memory.cognitive_production_migration._capture_code_state",
            return_value=clean_code_state(),
        ), patch(
            "memory.cognitive_production_migration._checkpoint",
            side_effect=checkpoint,
        ):
            return _execute_bound_cognitive_migration(
                self.paths,
                current_time=NOW,
                **clone_execution_kwargs(),
            )

    def test_precommit_failure_rolls_back_and_consumes_authority(self) -> None:
        with self.assertRaisesRegex(
            CognitiveProductionMigrationError,
            "injected_migration_failure",
        ) as captured:
            self.run_with_fault("before_commit")
        self.assertFalse(captured.exception.recovery_required)
        states = [item["state"] for item in _read_ledger(self.paths.ledger)]
        self.assertEqual(["claimed", "rolled_back"], states)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='cognitive_schema'"
                ).fetchone()
            )
        with patch(
            "memory.cognitive_production_migration._capture_code_state",
            return_value=clean_code_state(),
        ):
            with self.assertRaisesRegex(
                CognitiveProductionMigrationError,
                "one_time_migration_authority_replayed",
            ):
                _execute_bound_cognitive_migration(
                    self.paths,
                    current_time=NOW,
                    **clone_execution_kwargs(),
                )

    def test_after_commit_fault_is_reconciled_without_replaying_ddl(self) -> None:
        outcome = self.run_with_fault("after_commit")
        self.assertEqual("completed_reconciled", outcome["status"])
        self.assertTrue(outcome["postcommit_reconciliation"])
        self.assertTrue(outcome["system_verified"])
        self.assertEqual(
            ["claimed", "completed"],
            [item["state"] for item in _read_ledger(self.paths.ledger)],
        )
        verify_cognitive_state_schema(self.database)

    @unittest.skipUnless(
        os.name == "nt", "mandatory share-mode publication pins are Windows"
    )
    def test_exception_reconciliation_completed_append_holds_pins(self) -> None:
        original_append = production_migration._append_ledger_state
        blocked: list[OSError] = []

        def append_with_attack(path: Path, **arguments):
            if arguments.get("state") == "completed":
                try:
                    with self.packaged_database.open("ab") as handle:
                        handle.write(b"must-be-denied")
                except OSError as exc:
                    blocked.append(exc)
            return original_append(path, **arguments)

        with patch(
            "memory.cognitive_production_migration._append_ledger_state",
            side_effect=append_with_attack,
        ):
            outcome = self.run_with_fault("after_commit")
        self.assertEqual("completed_reconciled", outcome["status"])
        self.assertEqual(1, len(blocked))
        self.assertTrue(
            getattr(blocked[0], "winerror", None) in {5, 32, 33}
            or getattr(blocked[0], "errno", None) == 13
        )
        verify_cognitive_state_schema(self.database)

    @unittest.skipUnless(
        os.name == "nt", "mandatory share-mode publication pins are Windows"
    )
    def test_explicit_reconciliation_completed_append_holds_pins(self) -> None:
        _append_ledger_state(
            self.paths.ledger,
            authority=self.authority,
            state="claimed",
            reason_code="authority_claimed",
            recovery_required=False,
            recorded_at=T3,
        )
        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            _apply_fixed_cognitive_ddl(
                connection,
                plan=self.plan,
                created_at=T3,
            )
            connection.commit()
        manifest = self.package / "release_manifest.json"
        original_append = production_migration._append_ledger_state
        blocked: list[OSError] = []

        def append_with_attack(path: Path, **arguments):
            if arguments.get("state") == "completed":
                try:
                    with manifest.open("ab") as handle:
                        handle.write(b"must-be-denied")
                except OSError as exc:
                    blocked.append(exc)
            return original_append(path, **arguments)

        with patch(
            "memory.cognitive_production_migration._append_ledger_state",
            side_effect=append_with_attack,
        ):
            outcome = _reconcile_bound_cognitive_migration(
                self.paths,
                current_time=NOW,
                _test_package_verifier=clone_package_verifier,
            )
        self.assertEqual("completed_reconciled", outcome["status"])
        self.assertEqual(1, len(blocked))
        self.assertTrue(
            getattr(blocked[0], "winerror", None) in {5, 32, 33}
            or getattr(blocked[0], "errno", None) == 13
        )
        self.assertEqual(
            ["claimed", "completed"],
            [item["state"] for item in _read_ledger(self.paths.ledger)],
        )

    def test_completed_reconcile_requires_recoverable_bound_backup(self) -> None:
        with patch(
            "memory.cognitive_production_migration._capture_code_state",
            return_value=clean_code_state(),
        ):
            outcome = _execute_bound_cognitive_migration(
                self.paths,
                current_time=NOW,
                **clone_execution_kwargs(),
            )
        self.assertEqual("completed", outcome["status"])
        with self.packaged_database.open("ab") as handle:
            handle.write(b"corrupt-after-completed-ledger")
        reconciled = _reconcile_bound_cognitive_migration(
            self.paths,
            current_time=NOW,
            _test_package_verifier=clone_package_verifier,
        )
        self.assertEqual("recovery_required", reconciled["status"])
        self.assertEqual(
            "reconciliation_backup_revalidation_failed",
            reconciled["reason_code"],
        )
        self.assertEqual(
            ["claimed", "completed", "recovery_required"],
            [item["state"] for item in _read_ledger(self.paths.ledger)],
        )

    def test_partial_postcrash_schema_is_recovery_required_not_replayed(self) -> None:
        _append_ledger_state(
            self.paths.ledger,
            authority=self.authority,
            state="claimed",
            reason_code="authority_claimed",
            recovery_required=False,
            recorded_at=T3,
        )
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(_COGNITIVE_DDL[0])
            digest = schema_fingerprint(connection)
            connection.execute(
                "UPDATE operational_schema SET schema_digest=? WHERE singleton=1",
                (digest,),
            )
            connection.commit()
        result = _reconcile_bound_cognitive_migration(
            self.paths,
            current_time=NOW,
            _test_package_verifier=clone_package_verifier,
        )
        self.assertEqual("recovery_required", result["status"])
        self.assertTrue(result["recovery_required"])
        self.assertEqual(
            COGNITIVE_PRODUCTION_ROLLBACK_POLICY,
            result["recovery_action"],
        )
        self.assertEqual(
            ["claimed", "recovery_required"],
            [item["state"] for item in _read_ledger(self.paths.ledger)],
        )

    def test_corrupt_ledger_fails_closed(self) -> None:
        _append_ledger_state(
            self.paths.ledger,
            authority=self.authority,
            state="claimed",
            reason_code="authority_claimed",
            recovery_required=False,
            recorded_at=T3,
        )
        record = json.loads(self.paths.ledger.read_text(encoding="utf-8"))
        record["record_digest"] = "0" * 64
        self.paths.ledger.write_text(json.dumps(record) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(
            CognitiveProductionMigrationError, "migration_ledger_invalid"
        ):
            _reconcile_bound_cognitive_migration(
                self.paths,
                current_time=NOW,
                _test_package_verifier=clone_package_verifier,
            )

    def test_stale_os_lock_marker_is_recoverable(self) -> None:
        with _execution_lock(
            self.paths.lock, self.authority["one_time_execution_id"]
        ):
            _append_ledger_state(
                self.paths.ledger,
                authority=self.authority,
                state="claimed",
                reason_code="authority_claimed",
                recovery_required=False,
                recorded_at=T3,
            )
        self.assertTrue(self.paths.lock.is_file())
        result = _reconcile_bound_cognitive_migration(
            self.paths,
            current_time=NOW,
            _test_package_verifier=clone_package_verifier,
        )
        self.assertEqual("rolled_back", result["status"])
        self.assertEqual(
            ["claimed", "rolled_back"],
            [item["state"] for item in _read_ledger(self.paths.ledger)],
        )

    def test_hard_exit_releases_os_lock_and_reconcile_never_replays(self) -> None:
        script = "\n".join(
            (
                "import json, os, sys",
                "from pathlib import Path",
                "from memory.cognitive_production_migration import "
                "_append_ledger_state, _execution_lock",
                "control = Path(sys.argv[1])",
                "authority = json.loads((control / "
                "'cognitive_migration_authority.json').read_text('utf-8'))",
                "lock = control / 'cognitive_migration_execution.lock'",
                "ledger = control / 'cognitive_migration_execution.jsonl'",
                "manager = _execution_lock("
                "lock, authority['one_time_execution_id'])",
                "manager.__enter__()",
                "_append_ledger_state(ledger, authority=authority, "
                "state='claimed', reason_code='authority_claimed', "
                "recovery_required=False, "
                "recorded_at='2026-08-01T00:03:00.000000Z')",
                "os._exit(23)",
            )
        )
        completed = subprocess.run(
            (sys.executable, "-B", "-c", script, str(self.paths.lock.parent)),
            cwd=Path(__file__).resolve().parents[1],
            check=False,
            timeout=30,
        )
        self.assertEqual(23, completed.returncode)
        self.assertTrue(self.paths.lock.is_file())
        result = _reconcile_bound_cognitive_migration(
            self.paths,
            current_time=NOW,
            _test_package_verifier=clone_package_verifier,
        )
        self.assertEqual("rolled_back", result["status"])
        self.assertEqual(
            ["claimed", "rolled_back"],
            [item["state"] for item in _read_ledger(self.paths.ledger)],
        )
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='cognitive_schema'"
                ).fetchone()
            )

    def test_expired_authority_blocks_execute_but_not_readonly_reconcile(
        self,
    ) -> None:
        expired = datetime(2026, 8, 2, 0, 1, tzinfo=timezone.utc)
        with patch(
            "memory.cognitive_production_migration._capture_code_state",
            return_value=clean_code_state(),
        ), self.assertRaisesRegex(
            CognitiveProductionMigrationError,
            "authorization_expired",
        ):
            _execute_bound_cognitive_migration(
                self.paths,
                current_time=expired,
                **clone_execution_kwargs(),
            )
        self.assertFalse(self.paths.ledger.exists())
        _append_ledger_state(
            self.paths.ledger,
            authority=self.authority,
            state="claimed",
            reason_code="authority_claimed",
            recovery_required=False,
            recorded_at=T3,
        )
        result = _reconcile_bound_cognitive_migration(
            self.paths,
            current_time=expired,
            _test_package_verifier=clone_package_verifier,
        )
        self.assertEqual("rolled_back", result["status"])
        self.assertFalse(result["recovery_required"])

    def test_completed_ledger_does_not_hide_later_schema_drift(self) -> None:
        with patch(
            "memory.cognitive_production_migration._capture_code_state",
            return_value=clean_code_state(),
        ):
            _execute_bound_cognitive_migration(
                self.paths,
                current_time=NOW,
                **clone_execution_kwargs(),
            )
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("DROP INDEX objective_contracts_project_order")
            digest = schema_fingerprint(connection)
            connection.execute(
                "UPDATE operational_schema SET schema_digest=? WHERE singleton=1",
                (digest,),
            )
            connection.commit()
        result = _reconcile_bound_cognitive_migration(
            self.paths,
            current_time=NOW,
            _test_package_verifier=clone_package_verifier,
        )
        self.assertEqual("recovery_required", result["status"])
        self.assertTrue(result["recovery_required"])
        self.assertEqual("completed_migration_state_drift", result["reason_code"])

    def test_completed_migration_remains_verifiable_after_authorized_data_growth(self) -> None:
        with patch(
            "memory.cognitive_production_migration._capture_code_state",
            return_value=clean_code_state(),
        ):
            _execute_bound_cognitive_migration(
                self.paths,
                current_time=NOW,
                **clone_execution_kwargs(),
            )
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "INSERT INTO state_source_anchors VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    "anchor:v10-f-postmigration",
                    "test",
                    "test:v10-f-postmigration",
                    "a" * 64,
                    T3,
                    "private_metadata",
                    "available",
                    "{}",
                    "b" * 64,
                ),
            )
            connection.commit()
        result = _reconcile_bound_cognitive_migration(
            self.paths,
            current_time=NOW,
            _test_package_verifier=clone_package_verifier,
        )
        self.assertEqual("already_completed", result["status"])
        self.assertTrue(result["system_verified"])
        self.assertFalse(result["recovery_required"])

    def test_recovery_required_can_only_close_after_source_backup_restore(self) -> None:
        source_backup = self.root / "verified-source-backup.db"
        shutil.copy2(self.database, source_backup)
        _append_ledger_state(
            self.paths.ledger,
            authority=self.authority,
            state="claimed",
            reason_code="authority_claimed",
            recovery_required=False,
            recorded_at=T3,
        )
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(_COGNITIVE_DDL[0])
            digest = schema_fingerprint(connection)
            connection.execute(
                "UPDATE operational_schema SET schema_digest=? WHERE singleton=1",
                (digest,),
            )
            connection.commit()
        first = _reconcile_bound_cognitive_migration(
            self.paths,
            current_time=NOW,
            _test_package_verifier=clone_package_verifier,
        )
        self.assertEqual("recovery_required", first["status"])
        shutil.copy2(source_backup, self.database)
        second = _reconcile_bound_cognitive_migration(
            self.paths,
            current_time=NOW,
            _test_package_verifier=clone_package_verifier,
        )
        self.assertEqual("restored_verified", second["status"])
        self.assertFalse(second["recovery_required"])
        self.assertEqual(
            ["claimed", "recovery_required", "restored"],
            [item["state"] for item in _read_ledger(self.paths.ledger)],
        )

    def test_recovery_policy_forbids_reverse_ddl_and_automatic_replay(self) -> None:
        policy = cognitive_production_recovery_policy()
        self.assertEqual(
            COGNITIVE_PRODUCTION_ROLLBACK_POLICY,
            policy["rollback_policy"],
        )
        self.assertTrue(policy["verified_backup_required"])
        self.assertFalse(policy["destructive_schema_rollback_allowed"])
        self.assertFalse(policy["drop_cognitive_schema_allowed"])
        self.assertFalse(policy["automatic_replay_allowed"])
        ddl = "\n".join(_COGNITIVE_DDL).upper()
        self.assertNotIn("DROP TABLE", ddl)
        self.assertNotIn("ALTER TABLE", ddl)


if __name__ == "__main__":
    unittest.main()
