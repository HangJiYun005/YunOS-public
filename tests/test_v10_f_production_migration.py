from __future__ import annotations

import inspect
import hashlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from memory import cognitive_production_migration as production_migration
from memory.cognitive_production_migration import (
    COGNITIVE_DDL_DIGEST,
    COGNITIVE_PRODUCTION_ROLLBACK_POLICY,
    CognitiveProductionMigrationError,
    _execute_bound_cognitive_migration,
    _execution_lock,
    _fixed_production_paths,
    _read_ledger,
    _reconcile_bound_cognitive_migration,
    build_cognitive_migration_backup_verification,
    build_cognitive_migration_plan_for_clone,
    execute_cognitive_production_migration,
    reconcile_cognitive_production_migration,
)
from memory.cognitive_state_migration import (
    COGNITIVE_SCHEMA_GENERATION,
    verify_cognitive_state_schema,
)
from memory.operational_state_store import OperationalStateStore
from memory.operational_state_store import schema_fingerprint
from tests.v10_f_production_migration_fixtures import (
    NOW,
    build_bound_fixture,
    clean_code_state,
    clone_execution_kwargs,
    clone_live_gate_probe,
    clone_package_verifier,
    clone_tailscale_gate_probe,
    write_control_document,
)
from tools.run_authorized_v10_cognitive_migration import (
    build_parser as build_migration_cli_parser,
    main as migration_cli_main,
)
from tools.v10_release_recovery import (
    DEFAULT_ASSET_ROOT,
    DEFAULT_DATABASES,
    DEFAULT_RELEASE_REPORT_ROOT,
    _build_package_for_test as build_release_package_for_test,
    verify_package as verify_release_package,
)


class V10FProductionMigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="ayunos-v10f-migration-")
        self.root = Path(self.temp.name)
        fixture = build_bound_fixture(self.root)
        self.database = fixture["database"]
        self.paths = fixture["paths"]
        self.plan = fixture["plan"]
        self.backup = fixture["backup"]
        self.authority = fixture["authority"]
        self.package = fixture["package"]
        self.packaged_database = fixture["packaged_database"]
        self.capture = fixture["capture"]

    def tearDown(self) -> None:
        self.temp.cleanup()

    def execute(self, *, current_time=NOW):
        with patch(
            "memory.cognitive_production_migration._capture_code_state",
            return_value=clean_code_state(),
        ):
            return _execute_bound_cognitive_migration(
                self.paths,
                current_time=current_time,
                **clone_execution_kwargs(),
            )

    def test_deleted_or_corrupt_operational_backup_fails_before_claim(self) -> None:
        for corruption, expected in (
            ("deleted", "preproduction_operational_backup_missing"),
            ("corrupt", "preproduction_operational_backup_changed"),
        ):
            with self.subTest(corruption=corruption):
                fixture_root = self.root / corruption
                fixture_root.mkdir()
                fixture = build_bound_fixture(fixture_root)
                if corruption == "deleted":
                    fixture["packaged_database"].unlink()
                else:
                    with fixture["packaged_database"].open("ab") as handle:
                        handle.write(b"corrupt")
                with patch(
                    "memory.cognitive_production_migration._capture_code_state",
                    return_value=clean_code_state(),
                ), self.assertRaisesRegex(
                    CognitiveProductionMigrationError, expected
                ):
                    _execute_bound_cognitive_migration(
                        fixture["paths"],
                        current_time=NOW,
                        **clone_execution_kwargs(),
                    )
                self.assertFalse(fixture["paths"].ledger.exists())

    def test_stale_quiescence_evidence_fails_before_claim(self) -> None:
        stale_now = NOW.replace(second=1)
        with self.assertRaisesRegex(
            CognitiveProductionMigrationError,
            "service_quiescence_evidence_stale",
        ):
            self.execute(current_time=stale_now)
        self.assertFalse(self.paths.ledger.exists())

    def test_each_live_gate_is_rechecked_immediately_before_claim(self) -> None:
        failures = (
            ("local_service_listener_count", 1, "live_service_listener_present"),
            ("writer_lease_count", 1, "live_writer_lease_present"),
            ("operational_wal_size", 1, "live_operational_wal_nonempty"),
            (
                "production_write_env_enabled",
                True,
                "live_production_write_env_enabled",
            ),
        )
        for index, (field, bad_value, reason) in enumerate(failures):
            with self.subTest(field=field):
                fixture_root = self.root / f"live-{index}"
                fixture_root.mkdir()
                fixture = build_bound_fixture(fixture_root)
                gate = clone_live_gate_probe(fixture["database"])
                gate[field] = bad_value
                with patch(
                    "memory.cognitive_production_migration._capture_code_state",
                    return_value=clean_code_state(),
                ), self.assertRaisesRegex(
                    CognitiveProductionMigrationError, reason
                ):
                    _execute_bound_cognitive_migration(
                        fixture["paths"],
                        current_time=NOW,
                        _test_package_verifier=clone_package_verifier,
                        _test_live_gate_probe=lambda _path, value=gate: value,
                        _test_tailscale_gate_probe=clone_tailscale_gate_probe,
                    )
                self.assertFalse(fixture["paths"].ledger.exists())

    def test_production_entry_rejects_private_clone_test_seams(self) -> None:
        cases = (
            {
                "_test_package_verifier": clone_package_verifier,
                "_test_tailscale_gate_probe": clone_tailscale_gate_probe,
            },
            {"_test_source_identity_probe": lambda: {}},
            {"current_time": NOW},
        )
        for arguments in cases:
            with self.subTest(arguments=sorted(arguments)), self.assertRaisesRegex(
                CognitiveProductionMigrationError,
                "production_test_seam_forbidden",
            ):
                _execute_bound_cognitive_migration(
                    _fixed_production_paths(), **arguments
                )
        with self.assertRaisesRegex(
            CognitiveProductionMigrationError,
            "production_test_seam_forbidden",
        ):
            _reconcile_bound_cognitive_migration(
                _fixed_production_paths(), current_time=NOW
            )

    def test_private_production_cores_fail_closed_off_windows_before_io(self) -> None:
        production_paths = _fixed_production_paths()
        with patch.object(production_migration.os, "name", "posix"):
            for operation in (
                lambda: _execute_bound_cognitive_migration(production_paths),
                lambda: _reconcile_bound_cognitive_migration(production_paths),
            ):
                with self.subTest(operation=operation), self.assertRaisesRegex(
                    CognitiveProductionMigrationError,
                    "production_platform_unsupported",
                ):
                    operation()

    def test_plan_is_9001_core_plus_one_additive_10001_extension(self) -> None:
        self.assertEqual(
            "fc32daa94baec04484a59983b9fd539c0903644eea3aaa68fd63b54a82cf0b16",
            COGNITIVE_DDL_DIGEST,
        )
        self.assertEqual(9001, self.plan["core_schema_version"])
        self.assertEqual(10001, self.plan["target_schema_generation"])
        self.assertEqual(45, self.plan["expected_cognitive_object_count"])
        self.assertTrue(self.plan["additive_only"])
        self.assertFalse(self.plan["destructive_ddl_allowed"])
        self.assertEqual(
            COGNITIVE_PRODUCTION_ROLLBACK_POLICY,
            self.plan["rollback_policy"],
        )

    def test_verified_preproduction_package_binds_migration_backup_record(
        self,
    ) -> None:
        project = self.root / "preproduction-project"
        for label, relative in DEFAULT_DATABASES.items():
            target = project / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if label == "operational":
                shutil.copy2(self.database, target)
            else:
                with closing(sqlite3.connect(target)) as connection:
                    connection.execute(
                        "CREATE TABLE records(id INTEGER PRIMARY KEY,value TEXT)"
                    )
                    connection.execute(
                        "INSERT INTO records(value) VALUES(?)", (label,)
                    )
                    connection.commit()
        assets = project / DEFAULT_ASSET_ROOT
        assets.mkdir(parents=True)
        (assets / "migration-evidence.txt").write_text(
            "v10-f preproduction backup evidence\n", encoding="utf-8"
        )
        reports = project / DEFAULT_RELEASE_REPORT_ROOT
        reports.mkdir(parents=True)
        (reports / "yunos_v10_f_migration_integration.md").write_text(
            "# v10-F migration integration\n", encoding="utf-8"
        )

        operational = project / DEFAULT_DATABASES["operational"]
        OperationalStateStore(operational).create_snapshot(
            created_at="2026-08-01T00:00:30.000000Z"
        )
        plan = build_cognitive_migration_plan_for_clone(operational)
        package = self.root / "preproduction-package"
        manifest = build_release_package_for_test(
            phase="preproduction",
            output=package,
            _test_project_root=project,
            rehearsal_port=18773,
            service_quiescence_digest="d" * 64,
        )
        verified = verify_release_package(
            package, expected_phase="preproduction"
        )
        operational_backup = manifest["database_results"]["operational"][
            "backup_audit"
        ]["file_gate"]
        backup_record = build_cognitive_migration_backup_verification(
            plan=plan,
            backup_manifest_digest=verified["manifest_digest"],
            backup_set_digest=verified["backup_set_digest"],
            backup_database_sha256=operational_backup["sha256"],
            backup_database_size=operational_backup["size"],
            preproduction_package_absolute_path=str(package.absolute()),
            operational_backup_relative_path=manifest["database_results"][
                "operational"
            ]["backup_relative_path"],
            release_manifest_file_gate={
                "exists": True,
                "size": (package / "release_manifest.json").stat().st_size,
                "sha256": hashlib.sha256(
                    (package / "release_manifest.json").read_bytes()
                ).hexdigest(),
            },
            service_quiescence_digest=verified[
                "service_quiescence_digest"
            ],
            service_quiescence_captured_at=(
                "2026-08-01T00:01:00.000000Z"
            ),
            capture_artifact_digest="1" * 64,
            capture_record_digest="2" * 64,
            capture_issuer_digest="3" * 64,
            capture_nonce="4" * 32,
            capture_source_identities_digest="5" * 64,
            capture_relative_path=(
                "output/v10-f-quiescence-evidence/"
                "service_quiescence_evidence_20260801T000100000000Z.json"
            ),
            verified_at="2026-08-01T00:01:00.000000Z",
            expires_at="2026-08-01T00:20:00.000000Z",
        )
        self.assertEqual(
            plan["source_database_sha256"],
            backup_record["source_database_sha256"],
        )
        self.assertEqual(
            operational_backup["sha256"],
            backup_record["backup_database_sha256"],
        )
        self.assertEqual(
            manifest["manifest_digest"],
            backup_record["backup_manifest_digest"],
        )

    def test_fixed_migration_executes_once_and_preserves_v9_core(self) -> None:
        before = OperationalStateStore(self.database).read_status()
        outcome = self.execute()
        after = OperationalStateStore(self.database).read_status()
        cognitive = verify_cognitive_state_schema(self.database)
        self.assertEqual("completed", outcome["status"])
        self.assertTrue(outcome["system_verified"])
        self.assertEqual("waiting_acceptance", outcome["acceptance_state"])
        self.assertEqual(9001, after["schema_version"])
        self.assertEqual(
            COGNITIVE_SCHEMA_GENERATION, cognitive["schema_generation"]
        )
        self.assertNotEqual(before["schema_digest"], after["schema_digest"])
        self.assertEqual(self.plan["target_schema_digest"], after["schema_digest"])
        states = [item["state"] for item in _read_ledger(self.paths.ledger)]
        self.assertEqual(["claimed", "completed"], states)
        with self.assertRaisesRegex(
            CognitiveProductionMigrationError,
            "one_time_migration_authority_replayed",
        ):
            self.execute()

    def test_public_entrypoints_expose_no_path_sql_force_or_rollback_seam(self) -> None:
        for entrypoint in (
            execute_cognitive_production_migration,
            reconcile_cognitive_production_migration,
        ):
            with self.subTest(entrypoint=entrypoint.__name__):
                self.assertEqual([], list(inspect.signature(entrypoint).parameters))
        source = inspect.getsource(execute_cognitive_production_migration)
        self.assertNotIn("sql=", source.lower())
        self.assertNotIn("force=", source.lower())
        self.assertNotIn("path=", source.lower())
        bound_source = inspect.getsource(_execute_bound_cognitive_migration)
        self.assertIn(
            "_capture_live_claim_boundary_and_source_identities()",
            bound_source,
        )
        self.assertNotIn("_capture_live_tailscale_boundary", bound_source)

    def test_cli_has_exact_confirmations_and_no_target_override(self) -> None:
        parser = build_migration_cli_parser()
        help_text = parser.format_help().lower()
        for forbidden in (
            "--path",
            "--production-db",
            "--sql",
            "--force",
            "--rollback-path",
        ):
            self.assertNotIn(forbidden, help_text)
        exact = [
            "execute",
            "--execute",
            "--confirm-target-generation",
            "10001",
            "--confirm-migration-id",
            "yunos-v10-operational-cognitive-schema-10001",
            "--confirm-ddl-digest",
            COGNITIVE_DDL_DIGEST,
            "--confirm-production-database",
            "data/operational/ayunos_operational_state.db",
            "--confirm-rollback-policy",
            COGNITIVE_PRODUCTION_ROLLBACK_POLICY,
        ]
        parsed = parser.parse_args(exact)
        self.assertEqual("execute", parsed.action)
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            parser.parse_args(exact[:-2] + ["destructive-drop"])

    def test_cli_dispatches_only_to_fixed_entrypoint(self) -> None:
        expected = {"status": "completed", "recovery_required": False}
        output = io.StringIO()
        arguments = [
            "execute",
            "--execute",
            "--confirm-target-generation",
            "10001",
            "--confirm-migration-id",
            "yunos-v10-operational-cognitive-schema-10001",
            "--confirm-ddl-digest",
            COGNITIVE_DDL_DIGEST,
            "--confirm-production-database",
            "data/operational/ayunos_operational_state.db",
            "--confirm-rollback-policy",
            COGNITIVE_PRODUCTION_ROLLBACK_POLICY,
        ]
        with patch(
            "tools.run_authorized_v10_cognitive_migration.execute_cognitive_production_migration",
            return_value=expected,
        ) as execute, redirect_stdout(output):
            self.assertEqual(0, migration_cli_main(arguments))
        execute.assert_called_once_with()
        self.assertEqual(expected, json.loads(output.getvalue())["result"])

    def test_tampered_ddl_or_backup_binding_fails_before_claim(self) -> None:
        tampered = dict(self.authority)
        tampered["ddl_digest"] = "0" * 64
        unsigned = dict(tampered)
        unsigned.pop("authority_digest")
        from memory.operational_state_models import canonical_digest

        tampered["authority_digest"] = canonical_digest(unsigned)
        write_control_document(self.paths.authority_record, tampered)
        with self.assertRaisesRegex(
            CognitiveProductionMigrationError, "migration_authority_invalid"
        ):
            self.execute()
        self.assertFalse(self.paths.ledger.exists())
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='cognitive_schema'"
                ).fetchone()
            )

    def test_fixed_authority_capture_plan_and_manifest_are_mandatory(self) -> None:
        from memory.operational_state_models import canonical_digest

        authority_cases = (
            ("authority_id", "authority:forged", "authority_id_invalid"),
            ("issued_by", "agent:forged", "authority_issuer_invalid"),
            (
                "user_authorization_text_digest",
                "f" * 64,
                "user_authorization_text_digest_invalid",
            ),
            (
                "one_time_execution_id",
                "execution:forged",
                "one_time_execution_id_invalid|migration_authority_invalid",
            ),
            (
                "expires_at",
                "2026-08-02T00:00:00.000000Z",
                "authorization_expired",
            ),
        )
        for index, (field, forged, reason) in enumerate(authority_cases):
            with self.subTest(authority_field=field):
                fixture_root = self.root / f"authority-{index}"
                fixture_root.mkdir()
                fixture = build_bound_fixture(fixture_root)
                authority = dict(fixture["authority"])
                authority[field] = forged
                unsigned = dict(authority)
                unsigned.pop("authority_digest")
                authority["authority_digest"] = canonical_digest(unsigned)
                write_control_document(
                    fixture["paths"].authority_record, authority
                )
                with patch(
                    "memory.cognitive_production_migration._capture_code_state",
                    return_value=clean_code_state(),
                ), self.assertRaisesRegex(
                    CognitiveProductionMigrationError, reason
                ):
                    _execute_bound_cognitive_migration(
                        fixture["paths"],
                        current_time=NOW,
                        **clone_execution_kwargs(),
                    )
                self.assertFalse(fixture["paths"].ledger.exists())

        for kind in ("raw-capture", "plan", "manifest", "missing-plan", "missing-manifest"):
            with self.subTest(control=kind):
                fixture_root = self.root / f"control-{kind}"
                fixture_root.mkdir()
                fixture = build_bound_fixture(fixture_root)
                expected = ""
                if kind == "raw-capture":
                    write_control_document(
                        fixture["paths"].quiescence_record,
                        fixture["evidence"],
                    )
                    expected = "capture_artifact_invalid"
                elif kind == "plan":
                    plan = dict(fixture["plan"])
                    plan["source_database_size"] += 1
                    unsigned = dict(plan)
                    unsigned.pop("plan_digest")
                    plan["plan_digest"] = canonical_digest(unsigned)
                    write_control_document(fixture["paths"].plan_record, plan)
                    expected = "recorded_migration_plan_mismatch"
                elif kind == "manifest":
                    manifest = dict(fixture["control_manifest"])
                    manifest["migration_executed"] = True
                    unsigned = dict(manifest)
                    unsigned.pop("manifest_digest")
                    manifest["manifest_digest"] = canonical_digest(unsigned)
                    write_control_document(
                        fixture["paths"].control_manifest, manifest
                    )
                    expected = "control_manifest_binding_mismatch"
                elif kind == "missing-plan":
                    fixture["paths"].plan_record.unlink()
                    expected = "production_control_input_identity_changed"
                else:
                    fixture["paths"].control_manifest.unlink()
                    expected = "production_control_input_identity_changed"
                with patch(
                    "memory.cognitive_production_migration._capture_code_state",
                    return_value=clean_code_state(),
                ), self.assertRaisesRegex(
                    CognitiveProductionMigrationError, expected
                ):
                    _execute_bound_cognitive_migration(
                        fixture["paths"],
                        current_time=NOW,
                        **clone_execution_kwargs(),
                    )
                self.assertFalse(fixture["paths"].ledger.exists())

    def test_active_writer_lease_is_rejected(self) -> None:
        OperationalStateStore(self.database).acquire_writer_lease(
            owner_id="owner:v10-f-conflict",
            token="v10-f-conflicting-writer-token",
            acquired_at="2026-08-01T00:02:30.000000Z",
            expires_at="2026-08-01T00:10:00.000000Z",
        )
        with self.assertRaisesRegex(
            CognitiveProductionMigrationError,
            "cognitive_migration_active_writer_lease",
        ):
            self.execute()
        self.assertFalse(self.paths.ledger.exists())

    def test_nonzero_wal_and_source_schema_drift_fail_before_claim(self) -> None:
        wal = Path(str(self.database) + "-wal")
        wal.write_bytes(b"unexpected-live-writer")
        with self.assertRaisesRegex(
            CognitiveProductionMigrationError,
            "production_operational_wal_not_quiescent",
        ):
            self.execute()
        self.assertFalse(self.paths.ledger.exists())
        wal.unlink()

        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("CREATE TABLE unexpected_v10f_drift(value TEXT)")
            digest = schema_fingerprint(connection)
            connection.execute(
                "UPDATE operational_schema SET schema_digest=? WHERE singleton=1",
                (digest,),
            )
            connection.commit()
        with self.assertRaisesRegex(
            CognitiveProductionMigrationError,
            "recorded_migration_plan_mismatch",
        ):
            self.execute()
        self.assertFalse(self.paths.ledger.exists())

    def test_fixed_lock_and_hardlinked_control_file_fail_closed(self) -> None:
        with _execution_lock(self.paths.lock, "execution:active-holder"):
            with self.assertRaisesRegex(
                CognitiveProductionMigrationError,
                "production_migration_lock_active",
            ) as captured:
                self.execute()
            self.assertTrue(captured.exception.recovery_required)
        self.assertTrue(self.paths.lock.is_file())

        external = self.root / "external-ledger.txt"
        external.write_text("must-not-change", encoding="utf-8")
        try:
            os.link(external, self.paths.ledger)
        except OSError as exc:
            self.skipTest(f"hard links unavailable: {exc}")
        with self.assertRaisesRegex(
            CognitiveProductionMigrationError,
            "migration_path_hardlink_forbidden",
        ):
            self.execute()
        self.assertEqual("must-not-change", external.read_text(encoding="utf-8"))

    def test_ledger_hardlink_inserted_after_initial_read_never_receives_append(
        self,
    ) -> None:
        external = self.root / "late-external-ledger.txt"
        external.write_text("must-not-change", encoding="utf-8")
        injected = False

        def checkpoint(name: str) -> None:
            nonlocal injected
            if name == "preflight_validated" and not injected:
                os.link(external, self.paths.ledger)
                injected = True

        with patch(
            "memory.cognitive_production_migration._capture_code_state",
            return_value=clean_code_state(),
        ), patch(
            "memory.cognitive_production_migration._checkpoint",
            side_effect=checkpoint,
        ), self.assertRaisesRegex(
            CognitiveProductionMigrationError,
            "migration_ledger_invalid",
        ):
            _execute_bound_cognitive_migration(
                self.paths,
                current_time=NOW,
                **clone_execution_kwargs(),
            )
        self.assertTrue(injected)
        self.assertEqual("must-not-change", external.read_text(encoding="utf-8"))

    @unittest.skipUnless(
        os.name == "nt", "mandatory control ancestry pins are Windows"
    )
    def test_control_file_and_root_mutation_are_denied_for_full_execution(self) -> None:
        for kind in ("authority", "root"):
            with self.subTest(kind=kind):
                fixture_root = self.root / f"control-pin-{kind}"
                fixture_root.mkdir()
                fixture = build_bound_fixture(fixture_root)
                blocked = False

                def checkpoint(name: str) -> None:
                    nonlocal blocked
                    if name != "preflight_validated":
                        return
                    try:
                        if kind == "authority":
                            with fixture["paths"].authority_record.open("ab") as handle:
                                handle.write(b"forged")
                        else:
                            os.replace(
                                fixture["control"],
                                fixture_root / "moved-control",
                            )
                    except OSError:
                        blocked = True

                with patch(
                    "memory.cognitive_production_migration._capture_code_state",
                    return_value=clean_code_state(),
                ), patch(
                    "memory.cognitive_production_migration._checkpoint",
                    side_effect=checkpoint,
                ):
                    outcome = _execute_bound_cognitive_migration(
                        fixture["paths"],
                        current_time=NOW,
                        **clone_execution_kwargs(),
                    )
                self.assertTrue(blocked)
                self.assertTrue(outcome["system_verified"])
                self.assertEqual(
                    ["claimed", "completed"],
                    [
                        record["state"]
                        for record in _read_ledger(fixture["paths"].ledger)
                    ],
                )

    def test_writer_appearing_after_claim_is_rechecked_inside_begin_immediate(self) -> None:
        injected = False

        def checkpoint(name: str) -> None:
            nonlocal injected
            if name == "authority_claimed" and not injected:
                injected = True
                with closing(sqlite3.connect(self.database)) as connection:
                    connection.execute(
                        "INSERT INTO state_writer_leases VALUES(?,?,?,?,?)",
                        (
                            "operational-state-writer",
                            "f" * 64,
                            "owner:v10-f-race",
                            "2026-08-01T00:02:30.000000Z",
                            "2026-08-01T00:10:00.000000Z",
                        ),
                    )
                    connection.commit()

        with patch(
            "memory.cognitive_production_migration._capture_code_state",
            return_value=clean_code_state(),
        ), patch(
            "memory.cognitive_production_migration._checkpoint",
            side_effect=checkpoint,
        ):
            with self.assertRaises(CognitiveProductionMigrationError) as captured:
                _execute_bound_cognitive_migration(
                    self.paths,
                    current_time=NOW,
                    **clone_execution_kwargs(),
                )
        self.assertTrue(captured.exception.recovery_required)
        states = [item["state"] for item in _read_ledger(self.paths.ledger)]
        self.assertEqual(["claimed", "recovery_required"], states)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='cognitive_schema'"
                ).fetchone()
            )

    def test_backup_and_manifest_are_revalidated_immediately_before_claim(
        self,
    ) -> None:
        for kind in ("database", "manifest"):
            with self.subTest(kind=kind):
                fixture_root = self.root / f"claim-race-{kind}"
                fixture_root.mkdir()
                fixture = build_bound_fixture(fixture_root)

                def checkpoint(name: str) -> None:
                    if name != "preflight_validated":
                        return
                    target = (
                        fixture["packaged_database"]
                        if kind == "database"
                        else fixture["package"] / "release_manifest.json"
                    )
                    with target.open("ab") as handle:
                        handle.write(b"tampered-before-claim")

                with patch(
                    "memory.cognitive_production_migration._capture_code_state",
                    return_value=clean_code_state(),
                ), patch(
                    "memory.cognitive_production_migration._checkpoint",
                    side_effect=checkpoint,
                ), self.assertRaises(CognitiveProductionMigrationError):
                    _execute_bound_cognitive_migration(
                        fixture["paths"],
                        current_time=NOW,
                        **clone_execution_kwargs(),
                    )
                self.assertFalse(fixture["paths"].ledger.exists())
                with closing(sqlite3.connect(fixture["database"])) as connection:
                    self.assertIsNone(
                        connection.execute(
                            "SELECT 1 FROM sqlite_master "
                            "WHERE name='cognitive_schema'"
                        ).fetchone()
                    )

    @unittest.skipUnless(
        os.name == "nt", "mandatory recovery-input pins are Windows"
    )
    def test_postcommit_backup_or_manifest_mutation_is_blocked(self) -> None:
        for kind in ("database", "manifest"):
            with self.subTest(kind=kind):
                fixture_root = self.root / f"postcommit-race-{kind}"
                fixture_root.mkdir()
                fixture = build_bound_fixture(fixture_root)
                target = (
                    fixture["packaged_database"]
                    if kind == "database"
                    else fixture["package"] / "release_manifest.json"
                )
                original = target.read_bytes()
                mutation_blocked = False

                def checkpoint(name: str) -> None:
                    nonlocal mutation_blocked
                    if name == "after_commit":
                        try:
                            with target.open("ab") as handle:
                                handle.write(b"tampered-after-commit")
                        except OSError:
                            mutation_blocked = True

                with patch(
                    "memory.cognitive_production_migration._capture_code_state",
                    return_value=clean_code_state(),
                ), patch(
                    "memory.cognitive_production_migration._checkpoint",
                    side_effect=checkpoint,
                ):
                    outcome = _execute_bound_cognitive_migration(
                        fixture["paths"],
                        current_time=NOW,
                        **clone_execution_kwargs(),
                    )
                self.assertTrue(mutation_blocked)
                self.assertEqual(original, target.read_bytes())
                self.assertTrue(outcome["system_verified"])
                self.assertEqual(
                    ["claimed", "completed"],
                    [
                        item["state"]
                        for item in _read_ledger(fixture["paths"].ledger)
                    ],
                )
                verify_cognitive_state_schema(fixture["database"])

    @unittest.skipUnless(
        os.name == "nt", "mandatory recovery-input pins are Windows"
    )
    def test_recovery_backup_pin_precedes_claim_and_spans_transaction(self) -> None:
        for stage in ("authority_claimed", "transaction_started"):
            with self.subTest(stage=stage):
                fixture_root = self.root / f"preclaim-pin-{stage}"
                fixture_root.mkdir()
                fixture = build_bound_fixture(fixture_root)
                original = fixture["packaged_database"].read_bytes()
                blocked = False

                def checkpoint(name: str) -> None:
                    nonlocal blocked
                    if name == stage:
                        try:
                            with fixture["packaged_database"].open("ab") as handle:
                                handle.write(b"must-be-denied")
                        except OSError:
                            blocked = True

                with patch(
                    "memory.cognitive_production_migration._capture_code_state",
                    return_value=clean_code_state(),
                ), patch(
                    "memory.cognitive_production_migration._checkpoint",
                    side_effect=checkpoint,
                ):
                    outcome = _execute_bound_cognitive_migration(
                        fixture["paths"],
                        current_time=NOW,
                        **clone_execution_kwargs(),
                    )
                self.assertTrue(blocked)
                self.assertEqual(
                    original, fixture["packaged_database"].read_bytes()
                )
                self.assertTrue(outcome["system_verified"])
                self.assertEqual(
                    ["claimed", "completed"],
                    [
                        record["state"]
                        for record in _read_ledger(fixture["paths"].ledger)
                    ],
                )

    @unittest.skipUnless(
        os.name == "nt", "mandatory recovery-input pins are Windows"
    )
    def test_postcommit_verified_backup_drift_is_blocked(self) -> None:
        for kind in ("database", "manifest"):
            with self.subTest(kind=kind):
                fixture_root = self.root / f"final-backup-race-{kind}"
                fixture_root.mkdir()
                fixture = build_bound_fixture(fixture_root)
                target = (
                    fixture["packaged_database"]
                    if kind == "database"
                    else fixture["package"] / "release_manifest.json"
                )
                original = target.read_bytes()
                mutation_blocked = False

                def checkpoint(name: str) -> None:
                    nonlocal mutation_blocked
                    if name == "postcommit_verified":
                        try:
                            with target.open("ab") as handle:
                                handle.write(b"tampered-at-final-ledger-window")
                        except OSError:
                            mutation_blocked = True

                with patch(
                    "memory.cognitive_production_migration._capture_code_state",
                    return_value=clean_code_state(),
                ), patch(
                    "memory.cognitive_production_migration._checkpoint",
                    side_effect=checkpoint,
                ):
                    outcome = _execute_bound_cognitive_migration(
                        fixture["paths"],
                        current_time=NOW,
                        **clone_execution_kwargs(),
                    )
                self.assertTrue(mutation_blocked)
                self.assertEqual(original, target.read_bytes())
                self.assertTrue(outcome["system_verified"])
                self.assertEqual(
                    ["claimed", "completed"],
                    [
                        item["state"]
                        for item in _read_ledger(fixture["paths"].ledger)
                    ],
                )
                verify_cognitive_state_schema(fixture["database"])

    @unittest.skipUnless(
        os.name == "nt", "mandatory live-database identity pin is Windows"
    )
    def test_postcommit_verified_target_replacement_is_blocked(self) -> None:
        fixture_root = self.root / "final-target-replacement"
        fixture_root.mkdir()
        fixture = build_bound_fixture(fixture_root)
        replacement = fixture_root / "unmigrated-replacement.db"
        shutil.copy2(fixture["database"], replacement)
        replacement_blocked = False

        def checkpoint(name: str) -> None:
            nonlocal replacement_blocked
            if name == "postcommit_verified":
                try:
                    os.replace(replacement, fixture["database"])
                except OSError:
                    replacement_blocked = True

        with patch(
            "memory.cognitive_production_migration._capture_code_state",
            return_value=clean_code_state(),
        ), patch(
            "memory.cognitive_production_migration._checkpoint",
            side_effect=checkpoint,
        ):
            outcome = _execute_bound_cognitive_migration(
                fixture["paths"],
                current_time=NOW,
                **clone_execution_kwargs(),
            )
        self.assertTrue(replacement_blocked)
        self.assertTrue(outcome["system_verified"])
        self.assertEqual(
            ["claimed", "completed"],
            [item["state"] for item in _read_ledger(fixture["paths"].ledger)],
        )
        with closing(sqlite3.connect(fixture["database"])) as connection:
            self.assertIsNotNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='cognitive_schema'"
                ).fetchone()
            )

    @unittest.skipUnless(
        os.name == "nt", "mandatory share-mode publication pins are Windows"
    )
    def test_completed_append_cannot_mutate_any_pinned_input(self) -> None:
        for kind in ("backup", "manifest", "database"):
            with self.subTest(kind=kind):
                fixture_root = self.root / f"append-pin-{kind}"
                fixture_root.mkdir()
                fixture = build_bound_fixture(fixture_root)
                protected_before = {
                    "backup": fixture["packaged_database"].read_bytes(),
                    "manifest": (
                        fixture["package"] / "release_manifest.json"
                    ).read_bytes(),
                }
                replacement = fixture_root / "unmigrated-replacement.db"
                shutil.copy2(fixture["database"], replacement)
                original_append = production_migration._append_ledger_state
                blocked: list[OSError] = []

                def append_with_real_attack(path: Path, **arguments):
                    if arguments.get("state") == "completed":
                        try:
                            if kind == "backup":
                                with fixture["packaged_database"].open("ab") as handle:
                                    handle.write(b"must-be-denied")
                            elif kind == "manifest":
                                with (
                                    fixture["package"] / "release_manifest.json"
                                ).open("ab") as handle:
                                    handle.write(b"must-be-denied")
                            else:
                                os.replace(replacement, fixture["database"])
                        except OSError as exc:
                            blocked.append(exc)
                    return original_append(path, **arguments)

                with patch(
                    "memory.cognitive_production_migration._capture_code_state",
                    return_value=clean_code_state(),
                ), patch(
                    "memory.cognitive_production_migration._append_ledger_state",
                    side_effect=append_with_real_attack,
                ):
                    outcome = _execute_bound_cognitive_migration(
                        fixture["paths"],
                        current_time=NOW,
                        **clone_execution_kwargs(),
                    )
                self.assertEqual("completed", outcome["status"])
                self.assertEqual(1, len(blocked))
                self.assertTrue(
                    getattr(blocked[0], "winerror", None) in {5, 32, 33}
                    or getattr(blocked[0], "errno", None) == 13
                )
                self.assertEqual(
                    ["claimed", "completed"],
                    [
                        item["state"]
                        for item in _read_ledger(fixture["paths"].ledger)
                    ],
                )
                verify_cognitive_state_schema(fixture["database"])
                self.assertEqual(
                    protected_before["backup"],
                    fixture["packaged_database"].read_bytes(),
                )
                self.assertEqual(
                    protected_before["manifest"],
                    (fixture["package"] / "release_manifest.json").read_bytes(),
                )
                # Returning from the completed publication releases every pin.
                target = (
                    fixture["packaged_database"]
                    if kind == "backup"
                    else fixture["package"] / "release_manifest.json"
                    if kind == "manifest"
                    else fixture["database"]
                )
                with target.open("r+b"):
                    pass

    def test_claim_rechecks_code_state_and_tailscale_boundary(self) -> None:
        dirty = dict(clean_code_state())
        dirty["untracked_empty"] = False
        dirty["worktree_clean"] = False
        with patch(
            "memory.cognitive_production_migration._capture_code_state",
            side_effect=(clean_code_state(), dirty),
        ), self.assertRaisesRegex(
            CognitiveProductionMigrationError,
            "untracked_worktree_not_empty",
        ):
            _execute_bound_cognitive_migration(
                self.paths,
                current_time=NOW,
                **clone_execution_kwargs(),
            )
        self.assertFalse(self.paths.ledger.exists())

        fixture_root = self.root / "tailscale-claim-drift"
        fixture_root.mkdir()
        fixture = build_bound_fixture(fixture_root)
        arguments = clone_execution_kwargs()
        arguments["_test_tailscale_gate_probe"] = lambda: {
            "tailscale_backend": "Running",
            "tailscale_serve_config_digest": "0" * 64,
            "funnel_enabled": False,
        }
        with patch(
            "memory.cognitive_production_migration._capture_code_state",
            return_value=clean_code_state(),
        ), self.assertRaisesRegex(
            CognitiveProductionMigrationError,
            "live_tailscale_boundary_changed",
        ):
            _execute_bound_cognitive_migration(
                fixture["paths"],
                current_time=NOW,
                **arguments,
            )
        self.assertFalse(fixture["paths"].ledger.exists())

    def test_claim_rechecks_every_sealed_capture_source_identity(self) -> None:
        for source_id in (
            "tailscale_executable",
            "capture_controller",
            "operational_database",
        ):
            with self.subTest(source_id=source_id):
                fixture_root = self.root / f"source-claim-drift-{source_id}"
                fixture_root.mkdir()
                fixture = build_bound_fixture(fixture_root)
                live_identities = json.loads(
                    json.dumps(
                        fixture["capture"]["issuer"]["source_identities"]
                    )
                )
                original_digest = live_identities[source_id]["sha256"]
                live_identities[source_id]["sha256"] = (
                    "9" * 64 if original_digest != "9" * 64 else "8" * 64
                )
                arguments = clone_execution_kwargs()
                arguments["_test_source_identity_probe"] = (
                    lambda value=live_identities: value
                )
                with patch(
                    "memory.cognitive_production_migration."
                    "_capture_code_state",
                    return_value=clean_code_state(),
                ), self.assertRaisesRegex(
                    CognitiveProductionMigrationError,
                    "capture_artifact_source_identity_changed_before_claim",
                ):
                    _execute_bound_cognitive_migration(
                        fixture["paths"],
                        current_time=NOW,
                        **arguments,
                    )
                self.assertFalse(fixture["paths"].ledger.exists())

    def test_runtime_claim_probe_consumes_one_combined_sealed_snapshot(
        self,
    ) -> None:
        expected = {
            "boundary": clone_tailscale_gate_probe(),
            "source_identities": self.capture["issuer"][
                "source_identities"
            ],
        }
        with patch(
            "tools.v10_f_capture_production_quiescence."
            "capture_fixed_claim_boundary_and_source_identities",
            return_value=expected,
        ) as combined_probe:
            probe = getattr(
                production_migration,
                "_capture_live_claim_boundary_and_source_identities",
            )
            actual = probe()
        self.assertEqual(expected, actual)
        combined_probe.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
