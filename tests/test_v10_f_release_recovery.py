from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from memory.cognitive_provider import build_suggested_provider_registry
from memory.cognitive_state_migration import apply_cognitive_state_migration
from memory.operational_state_models import canonical_digest, canonical_json
from memory.operational_state_store import (
    OperationalStateStore,
    initialize_operational_database,
)
from tools import v10_release_recovery as recovery
from tools import v10_f_isolated_restore_web_acceptance as isolated_acceptance
from tools.v10_release_recovery import (
    RecoveryError,
    build_package,
    restore_package,
    verify_package,
)


T0 = "2026-08-01T00:00:00.000000Z"
T1 = "2026-08-01T00:01:00.000000Z"
T2 = "2026-08-01T00:02:00.000000Z"
QUIESCENCE_DIGEST = "d" * 64


class V10FReleaseRecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="v10-f-release-recovery-",
            ignore_cleanup_errors=True,
        )
        self.root = Path(self.temporary.name).resolve(strict=True)
        self.project = self.root / "project"
        self.project.mkdir()
        for label, relative in recovery.DEFAULT_DATABASES.items():
            path = self.project / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if label == "operational":
                self._create_operational(path)
            else:
                with closing(sqlite3.connect(path)) as connection:
                    connection.execute(
                        "CREATE TABLE records(id INTEGER PRIMARY KEY,value TEXT)"
                    )
                    connection.execute(
                        "INSERT INTO records(value) VALUES(?)", (label,)
                    )
                    connection.commit()
        self.assets = self.project / recovery.DEFAULT_ASSET_ROOT
        self.assets.mkdir(parents=True)
        (self.assets / "asset-one.bin").write_bytes(b"one")
        nested = self.assets / "nested"
        nested.mkdir()
        (nested / "asset-two.bin").write_bytes(b"two")
        reports = self.project / recovery.DEFAULT_RELEASE_REPORT_ROOT
        reports.mkdir(parents=True)
        (reports / "yunos_v10_f_test_release.md").write_text(
            "# YunOS v10-F 临时恢复测试报告\n\n不含凭据。\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _create_operational(path: Path) -> None:
        initialize_operational_database(path, created_at=T0)
        apply_cognitive_state_migration(path, created_at=T1)
        registry = build_suggested_provider_registry(
            ("secret.deepseek.production",)
        )
        profile = registry.get("deepseek", 1)
        anchor = {
            "anchor_id": "anchor:v10-f-release-test",
            "source_kind": "user_declaration",
            "locator": "test:v10-f-release-recovery",
            "evidence_digest": "a" * 64,
            "observed_at": T0,
            "privacy_scope": "private_metadata",
        }
        anchor_digest = canonical_digest(anchor)
        payload = {"state": "approved", "scope": "v10-f-release-test"}
        payload_digest = canonical_digest(payload)
        objective = {
            "objective_id": "objective:v10-f-release-test",
            "version": 1,
            "project_id": "project:yunos",
            "agency_level": "act",
            "cognitive_stance": "continue",
        }
        objective_digest = canonical_digest(objective)
        plan = {
            "plan_id": "plan:v10-f-release-test",
            "version": 1,
            "objective_id": objective["objective_id"],
            "objective_version": 1,
        }
        plan_digest = canonical_digest(plan)
        event_id = "event:v10-f-release-test"
        entity_id = "decision:v10-f-release-test"
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN")
            connection.execute("PRAGMA defer_foreign_keys=ON")
            connection.execute(
                "INSERT INTO state_source_anchors VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    anchor["anchor_id"],
                    anchor["source_kind"],
                    anchor["locator"],
                    anchor["evidence_digest"],
                    anchor["observed_at"],
                    anchor["privacy_scope"],
                    "available",
                    canonical_json(anchor),
                    anchor_digest,
                ),
            )
            identity = {
                "entity_id": entity_id,
                "entity_type": "decision",
                "namespace": "yunos",
                "external_key": entity_id,
            }
            connection.execute(
                "INSERT INTO state_entities VALUES(?,?,?,?,?,?,?)",
                (
                    entity_id,
                    "decision",
                    "yunos",
                    entity_id,
                    canonical_digest(identity),
                    event_id,
                    T1,
                ),
            )
            connection.execute(
                "INSERT INTO state_events("
                "event_id,project_id,entity_id,entity_type,event_type,source_kind,"
                "source_anchor_id,authority_kind,authority_digest,actor_kind,"
                "actor_id,observed_at,effective_at,recorded_at,expires_at,"
                "supersedes_event_id,caused_by_event_id,idempotency_key,"
                "conflict_group_id,canonical_payload,payload_digest,privacy_scope,"
                "projection_version,schema_version"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    event_id,
                    "project:yunos",
                    entity_id,
                    "decision",
                    "decision_approved",
                    "declared",
                    anchor["anchor_id"],
                    "user",
                    "b" * 64,
                    "user",
                    "user:local",
                    T1,
                    T1,
                    T1,
                    None,
                    None,
                    None,
                    "idempotency:v10-f-release-test",
                    None,
                    canonical_json(payload),
                    payload_digest,
                    "private_metadata",
                    1,
                    9001,
                ),
            )
            connection.execute(
                "INSERT INTO objective_contracts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    objective["objective_id"],
                    1,
                    "project:yunos",
                    "act",
                    "continue",
                    0,
                    canonical_json(objective),
                    objective_digest,
                    anchor["anchor_id"],
                    event_id,
                    None,
                    T1,
                ),
            )
            connection.execute(
                "INSERT INTO provider_registry("
                "provider_id,profile_version,display_name,endpoint,model,enabled,"
                "manual_selection_only,secret_reference,health_state,"
                "canonical_profile,profile_digest,created_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    profile.provider_id,
                    profile.profile_version,
                    profile.display_name,
                    profile.endpoint,
                    profile.model,
                    int(profile.enabled),
                    1,
                    profile.secret_reference.reference_id,
                    profile.health_state,
                    canonical_json(profile.to_dict()),
                    profile.profile_digest,
                    T1,
                ),
            )
            connection.execute(
                "INSERT INTO plan_graphs VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    plan["plan_id"],
                    1,
                    objective["objective_id"],
                    1,
                    objective_digest,
                    canonical_json(plan),
                    plan_digest,
                    event_id,
                    T1,
                ),
            )
            connection.execute(
                "INSERT INTO cognitive_runs("
                "run_id,objective_id,objective_version,plan_id,plan_version,"
                "provider_id,provider_profile_version,authorization_id,status,"
                "checkpoint_sequence,last_safe_checkpoint_digest,model_calls_used,"
                "tokens_used,cost_used_microunits,steps_used,started_at,"
                "last_activity_at,ended_at,failure_code,current_projection_digest"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "run:v10-f-release-test",
                    objective["objective_id"],
                    1,
                    plan["plan_id"],
                    1,
                    profile.provider_id,
                    profile.profile_version,
                    None,
                    "waiting_acceptance",
                    0,
                    None,
                    1,
                    128,
                    0,
                    1,
                    T1,
                    T2,
                    None,
                    None,
                    "c" * 64,
                ),
            )
            connection.commit()
        OperationalStateStore(path).create_snapshot(created_at=T2)

    def _build(self, name: str = "package") -> tuple[Path, dict]:
        output = self.root / name
        manifest = recovery._build_package_for_test(
            phase="final",
            output=output,
            _test_project_root=self.project,
            rehearsal_port=18765,
            service_quiescence_digest=QUIESCENCE_DIGEST,
        )
        return output, manifest

    def test_build_verify_and_restore_five_database_package(self) -> None:
        output, manifest = self._build()
        self.assertEqual(manifest["database_count"], 5)
        self.assertFalse(manifest["production_write_performed"])
        self.assertTrue(manifest["restore_rehearsal"]["passed"])
        self.assertEqual(
            manifest["source_provenance"]["source_binding"], "test_fixture"
        )
        self.assertEqual(set(manifest["database_results"]), set(recovery.DEFAULT_DATABASES))
        for label, relative in recovery.DEFAULT_DATABASES.items():
            source = self.project / relative
            self.assertEqual(
                recovery._sqlite_file_gates(source),
                manifest["database_results"][label]["stable_source_file_gates"],
            )

        logical = json.loads(
            (output / "logical_summaries.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            logical["cognitive"]["counts"],
            {
                "objectives": 1,
                "plans": 1,
                "runs": 1,
                "operational_projections": 0,
                "delegations": 0,
                "outcomes": 0,
            },
        )
        provider_text = (output / "provider_configuration_template.json").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("secret.deepseek.production", provider_text)
        self.assertNotIn('"secret_reference":', provider_text)
        self.assertNotIn("api_key", provider_text.lower())
        self.assertTrue(
            (output / "release_reports/yunos_v10_f_test_release.md").is_file()
        )

        verified = verify_package(output, expected_phase="final")
        self.assertEqual(verified["status"], "verified")
        self.assertTrue(all(verified["database_checks"].values()))
        self.assertFalse(verified["provider_secret_material_included"])

        restored = self.root / "isolated-restore"
        result = restore_package(
            package=output,
            destination=restored,
            port=18766,
            expected_phase="final",
        )
        self.assertEqual(result["status"], "restored")
        self.assertFalse(result["production_database_connected"])
        runtime = json.loads(
            (restored / "restore_runtime.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            runtime["requested_loopback_origin"], "http://127.0.0.1:18766"
        )
        self.assertFalse(runtime["write_routes_enabled"])
        isolated_contract = isolated_acceptance._validate_restore_contract(
            restored,
            18766,
            output_root=self.root,
        )
        self.assertNotEqual(
            isolated_contract.manifest["ownership_marker_identity_digest"],
            isolated_contract.runtime["ownership_marker_identity_digest"],
        )
        self.assertEqual(
            isolated_contract.runtime["ownership_marker_identity_digest"],
            isolated_contract.ownership_marker["ownership_identity_digest"],
        )
        self.assertTrue(
            (
                restored
                / "recovery_metadata/release_reports/yunos_v10_f_test_release.md"
            ).is_file()
        )
        for label, relative in recovery.DEFAULT_DATABASES.items():
            self.assertEqual(
                recovery._hash_file(restored / relative),
                manifest["database_results"][label]["backup_audit"][
                    "file_gate"
                ]["sha256"],
            )

    def test_preproduction_package_is_phase_bound_and_restorable(self) -> None:
        project = self.root / "preproduction-project"
        shutil.copytree(self.project, project)
        operational = project / recovery.DEFAULT_DATABASES["operational"]
        for candidate in (
            operational,
            Path(str(operational) + "-wal"),
            Path(str(operational) + "-shm"),
        ):
            if candidate.is_file():
                candidate.unlink()
        initialize_operational_database(operational, created_at=T0)
        OperationalStateStore(operational).create_snapshot(created_at=T2)

        output = self.root / "preproduction-package"
        manifest = recovery._build_package_for_test(
            phase="preproduction",
            output=output,
            _test_project_root=project,
            rehearsal_port=18771,
            service_quiescence_digest=QUIESCENCE_DIGEST,
        )
        self.assertEqual(manifest["phase"], "preproduction")
        logical = json.loads(
            (output / "logical_summaries.json").read_text(encoding="utf-8")
        )
        self.assertEqual(logical["cognitive"]["availability"], "not_migrated")
        self.assertIsNone(logical["cognitive"]["counts"]["objectives"])
        provider = json.loads(
            (output / "provider_configuration_template.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(provider["availability"], "not_migrated")
        self.assertEqual(provider["profiles"], [])
        self.assertEqual(
            verify_package(output, expected_phase="preproduction")["status"],
            "verified",
        )
        with self.assertRaisesRegex(RecoveryError, "recovery_phase_mismatch"):
            verify_package(output, expected_phase="final")
        restored = self.root / "preproduction-restore"
        result = restore_package(
            package=output,
            destination=restored,
            port=18772,
            expected_phase="preproduction",
        )
        self.assertEqual(result["phase"], "preproduction")
        with self.assertRaisesRegex(
            RecoveryError, "recovery_phase_schema_mismatch"
        ):
            recovery._build_package_for_test(
                phase="final",
                output=self.root / "preproduction-as-final",
                _test_project_root=project,
                service_quiescence_digest=QUIESCENCE_DIGEST,
            )

    def test_wal_rows_are_included_and_sidecars_are_forensic_evidence(self) -> None:
        main = self.project / recovery.DEFAULT_DATABASES["main"]
        connection = sqlite3.connect(main)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("INSERT INTO records(value) VALUES('wal-row')")
            connection.commit()
            self.assertGreater(Path(str(main) + "-wal").stat().st_size, 0)
            output, manifest = self._build("wal-package")
            self.assertEqual(
                manifest["database_results"]["main"]["backup_audit"]["tables"][
                    "records"
                ]["row_count"],
                2,
            )
            wal_evidence = manifest["database_results"]["main"][
                "sqlite_sidecars"
            ]["wal"]
            self.assertTrue(wal_evidence["source"]["exists"])
            self.assertEqual(wal_evidence["source"], wal_evidence["packaged"])
            restored = self.root / "wal-restore"
            restore_package(
                package=output,
                destination=restored,
                port=18767,
                expected_phase="final",
            )
            with closing(
                sqlite3.connect(restored / recovery.DEFAULT_DATABASES["main"])
            ) as restored_connection:
                self.assertEqual(
                    restored_connection.execute(
                        "SELECT COUNT(*) FROM records"
                    ).fetchone()[0],
                    2,
                )
        finally:
            connection.close()

    def test_manifest_traversal_is_rejected_even_when_resigned(self) -> None:
        output, _manifest = self._build()
        manifest_path = output / "release_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["artifact_inventory"][0]["path"] = "../outside.bin"
        manifest["manifest_digest"] = canonical_digest(
            {
                key: value
                for key, value in manifest.items()
                if key != "manifest_digest"
            }
        )
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        marker_path = output / recovery._OWNER_MARKER
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        marker_path.write_text(
            canonical_json(
                recovery._sealed_owner_marker(
                    purpose="package",
                    nonce=marker["nonce"],
                    phase="final",
                    manifest_digest=manifest["manifest_digest"],
                    backup_set_digest=manifest["backup_set_digest"],
                    runtime_digest=None,
                )
            )
            + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            RecoveryError, "manifest_relative_path_invalid"
        ):
            verify_package(output, expected_phase="final")

    def test_artifact_tamper_fails_closed(self) -> None:
        output, _manifest = self._build()
        guide = output / "RECOVERY.md"
        guide.write_text(guide.read_text(encoding="utf-8") + "tamper\n", encoding="utf-8")
        with self.assertRaisesRegex(RecoveryError, "artifact_digest_mismatch"):
            verify_package(output, expected_phase="final")

    def test_database_tamper_between_verify_and_copy_fails_closed(self) -> None:
        output, manifest = self._build()
        database = output / manifest["database_results"]["main"][
            "backup_relative_path"
        ]
        original_verify = recovery.verify_package

        def verify_then_tamper(package: Path, *, expected_phase: str):
            result = original_verify(package, expected_phase=expected_phase)
            with database.open("ab") as handle:
                handle.write(b"x" * 4096)
            return result

        destination = self.root / "tampered-restore"
        with patch.object(recovery, "verify_package", side_effect=verify_then_tamper):
            with self.assertRaisesRegex(
                RecoveryError, "package_database_changed_before_restore"
            ):
                restore_package(
                    package=output,
                    destination=destination,
                    port=18774,
                    expected_phase="final",
                )
        self.assertFalse(destination.exists())

    def test_hardlinked_asset_source_is_rejected(self) -> None:
        outside = self.root / "outside-secret.bin"
        outside.write_bytes(b"PRIVATE-OUTSIDE-BOUNDARY")
        linked = self.assets / "outside-hardlink.bin"
        try:
            os.link(outside, linked)
        except OSError as exc:
            self.skipTest(f"hardlink unavailable in test environment: {exc}")
        output = self.root / "hardlink-package"
        with self.assertRaisesRegex(RecoveryError, "hardlink_path_forbidden"):
            recovery._build_package_for_test(
                phase="final",
                output=output,
                _test_project_root=self.project,
                service_quiescence_digest=QUIESCENCE_DIGEST,
            )
        self.assertFalse(output.exists())
        self.assertEqual(outside.read_bytes(), b"PRIVATE-OUTSIDE-BOUNDARY")

    def test_existing_destinations_and_production_ports_are_rejected(self) -> None:
        existing = self.root / "existing"
        existing.mkdir()
        with self.assertRaisesRegex(RecoveryError, "destination_exists"):
            recovery._build_package_for_test(
                phase="final",
                output=existing,
                _test_project_root=self.project,
                service_quiescence_digest=QUIESCENCE_DIGEST,
            )
        output, _manifest = self._build()
        with self.assertRaisesRegex(
            RecoveryError, "independent_restore_port_required"
        ):
            restore_package(
                package=output,
                destination=self.root / "blocked-restore",
                port=8765,
                expected_phase="final",
            )
        restore_target = self.root / "restore-existing"
        restore_target.mkdir()
        with self.assertRaisesRegex(RecoveryError, "destination_exists"):
            restore_package(
                package=output,
                destination=restore_target,
                port=18768,
                expected_phase="final",
            )

    def test_quiescence_digest_and_zero_writer_lease_are_required(self) -> None:
        with self.assertRaisesRegex(
            RecoveryError, "service_quiescence_digest_required"
        ):
            recovery._build_package_for_test(
                phase="final",
                output=self.root / "invalid-quiescence",
                _test_project_root=self.project,
                service_quiescence_digest="not-a-digest",
            )
        operational = self.project / recovery.DEFAULT_DATABASES["operational"]
        OperationalStateStore(operational).acquire_writer_lease(
            owner_id="owner:v10-f-recovery-test",
            token="v10-f-recovery-test-token",
            acquired_at=T1,
            expires_at="2026-08-01T01:00:00.000000Z",
        )
        with self.assertRaisesRegex(
            RecoveryError, "operational_source_not_quiescent"
        ):
            recovery._build_package_for_test(
                phase="final",
                output=self.root / "leased-package",
                _test_project_root=self.project,
                service_quiescence_digest=QUIESCENCE_DIGEST,
            )

    def test_package_and_restore_paths_cannot_overlap_inputs(self) -> None:
        with self.assertRaisesRegex(
            RecoveryError, "package_destination_overlaps_source"
        ):
            recovery._build_package_for_test(
                phase="final",
                output=self.assets / "forbidden-package",
                _test_project_root=self.project,
                service_quiescence_digest=QUIESCENCE_DIGEST,
            )
        output, _manifest = self._build()
        with self.assertRaisesRegex(
            RecoveryError, "restore_destination_overlaps_package"
        ):
            restore_package(
                package=output,
                destination=output / "forbidden-restore",
                port=18769,
                expected_phase="final",
            )
        sealed_data = Path(recovery.__file__).resolve().parents[1] / "data"
        forbidden_production_target = (
            sealed_data
            / f".v10-f-readonly-overlap-probe-{os.getpid()}-{id(self)}"
        )
        self.assertFalse(forbidden_production_target.exists())
        with self.assertRaisesRegex(
            RecoveryError, "independent_restore_path_required"
        ):
            restore_package(
                package=output,
                destination=forbidden_production_target,
                port=18770,
                expected_phase="final",
            )
        self.assertFalse(forbidden_production_target.exists())

    def test_resigned_provider_secret_injection_still_fails_closed(self) -> None:
        output, _manifest = self._build()
        provider_path = output / "provider_configuration_template.json"
        provider = json.loads(provider_path.read_text(encoding="utf-8"))
        provider["api_key"] = "sk-proj-abcdefghijklmnop"
        provider["template_digest"] = canonical_digest(
            {
                key: value
                for key, value in provider.items()
                if key != "template_digest"
            }
        )
        provider_path.write_text(
            json.dumps(provider, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )

        manifest_path = output / "release_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for item in manifest["artifact_inventory"]:
            if item["path"] == "provider_configuration_template.json":
                item["size"] = provider_path.stat().st_size
                item["sha256"] = recovery._hash_file(provider_path)
                break
        logical = json.loads(
            (output / "logical_summaries.json").read_text(encoding="utf-8")
        )
        manifest["backup_set_digest"] = canonical_digest(
            recovery._backup_set_basis(
                manifest["artifact_inventory"],
                manifest["database_results"],
                manifest["assets"],
                logical["cognitive"],
                provider,
                manifest["release_reports"],
                manifest["source_provenance"],
                manifest["service_quiescence"]["evidence_digest"],
                "final",
                manifest["ownership_marker_identity_digest"],
            )
        )
        manifest["manifest_digest"] = canonical_digest(
            {
                key: value
                for key, value in manifest.items()
                if key != "manifest_digest"
            }
        )
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        marker_path = output / recovery._OWNER_MARKER
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        marker_path.write_text(
            canonical_json(
                recovery._sealed_owner_marker(
                    purpose="package",
                    nonce=marker["nonce"],
                    phase="final",
                    manifest_digest=manifest["manifest_digest"],
                    backup_set_digest=manifest["backup_set_digest"],
                    runtime_digest=None,
                )
            )
            + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            RecoveryError, "provider_template_secret_detected"
        ):
            verify_package(output, expected_phase="final")

    def test_source_drift_aborts_and_cleans_only_owned_staging(self) -> None:
        original = recovery._online_backup
        main = (self.project / recovery.DEFAULT_DATABASES["main"]).resolve()

        def backup_and_mutate(source: Path, target: Path) -> None:
            original(source, target)
            if source == main:
                with closing(sqlite3.connect(source)) as connection:
                    connection.execute(
                        "INSERT INTO records(value) VALUES('concurrent-write')"
                    )
                    connection.commit()

        output = self.root / "drift-package"
        with patch.object(recovery, "_online_backup", side_effect=backup_and_mutate):
            with self.assertRaisesRegex(
                RecoveryError, "source_database_changed_during_backup"
            ):
                recovery._build_package_for_test(
                    phase="final",
                    output=output,
                    _test_project_root=self.project,
                    service_quiescence_digest=QUIESCENCE_DIGEST,
                )
        self.assertFalse(output.exists())
        self.assertEqual(list(self.root.glob(".yunos-v10-package-*")), [])
        self.assertTrue(self.project.is_dir())

    def test_reparse_asset_is_rejected_without_touching_source(self) -> None:
        linked = self.assets / "linked"
        linked.mkdir()
        (linked / "data.bin").write_bytes(b"linked")
        original = recovery._is_reparse

        def classify(path: Path) -> bool:
            return path == linked or original(path)

        output = self.root / "reparse-package"
        with patch.object(recovery, "_is_reparse", side_effect=classify):
            with self.assertRaisesRegex(RecoveryError, "reparse_path_forbidden"):
                recovery._build_package_for_test(
                    phase="final",
                    output=output,
                    _test_project_root=self.project,
                    service_quiescence_digest=QUIESCENCE_DIGEST,
                )
        self.assertFalse(output.exists())
        self.assertEqual((linked / "data.bin").read_bytes(), b"linked")

    def test_production_build_has_no_project_root_override_and_mixed_fixture_is_rejected(
        self,
    ) -> None:
        with self.assertRaises(TypeError):
            build_package(
                phase="final",
                output=self.root / "programmatic-override",
                project_root=self.project,  # type: ignore[call-arg]
                service_quiescence_digest=QUIESCENCE_DIGEST,
            )
        with patch("sys.stderr"):
            with self.assertRaises(SystemExit) as raised:
                recovery.main(
                    [
                        "build",
                        "--phase",
                        "final",
                        "--output",
                        str(self.root / "cli-override"),
                        "--project-root",
                        str(self.project),
                        "--service-quiescence-digest",
                        QUIESCENCE_DIGEST,
                    ]
                )
        self.assertEqual(raised.exception.code, 2)

        _package, manifest = self._build("mixed-source-package")
        with self.assertRaisesRegex(
            RecoveryError, "fixed_production_source_binding_required"
        ):
            recovery.validate_fixed_production_provenance(
                manifest["source_provenance"]
            )

    def test_fixed_production_provenance_validator_uses_only_fixed_root(self) -> None:
        anchored_root = Path(recovery.__file__).resolve().parents[1]
        sentinel = {"status": "sealed-root-dispatched"}
        with patch.object(
            recovery, "PROJECT_ROOT", self.project
        ), patch.object(
            recovery, "_build_package", return_value=sentinel
        ) as internal_build:
            self.assertIs(
                sentinel,
                build_package(
                    phase="final",
                    output=self.root / "fixed-root-package",
                    rehearsal_port=18778,
                    service_quiescence_digest=QUIESCENCE_DIGEST,
                ),
            )
        self.assertEqual(
            anchored_root,
            internal_build.call_args.kwargs["project_root"],
        )
        self.assertEqual(
            "fixed_project_root",
            internal_build.call_args.kwargs["source_binding"],
        )

        _package, fixture_manifest = self._build("root-anchor-record")
        recorded = json.loads(
            json.dumps(fixture_manifest["source_provenance"])
        )
        recorded["source_binding"] = "fixed_project_root"
        recorded["provenance_digest"] = canonical_digest(
            {
                key: value
                for key, value in recorded.items()
                if key != "provenance_digest"
            }
        )
        with patch.object(
            recovery, "PROJECT_ROOT", self.project
        ), patch.object(
            recovery,
            "_inspect_project_source_provenance",
            return_value=recorded,
        ) as inspect_source:
            report = recovery.validate_fixed_production_provenance(recorded)
        inspect_source.assert_called_once_with(
            anchored_root, source_binding="fixed_project_root"
        )
        self.assertEqual("matched", report["status"])

        with patch.object(recovery, "_build_package") as internal_build:
            with self.assertRaisesRegex(
                RecoveryError, "test_project_root_is_production"
            ):
                recovery._build_package_for_test(
                    phase="final",
                    output=self.root / "forbidden-real-root-fixture",
                    _test_project_root=anchored_root,
                    service_quiescence_digest=QUIESCENCE_DIGEST,
                )
        internal_build.assert_not_called()

    def test_main_late_drift_after_later_database_aborts_final_seal(self) -> None:
        original = recovery._online_backup
        main = (self.project / recovery.DEFAULT_DATABASES["main"]).resolve()
        profile = (
            self.project / recovery.DEFAULT_DATABASES["profile"]
        ).resolve()

        def backup_then_mutate_earlier_database(
            source: Path, target: Path
        ) -> None:
            original(source, target)
            if source == profile:
                with closing(sqlite3.connect(main)) as connection:
                    connection.execute(
                        "INSERT INTO records(value) VALUES('late-main-drift')"
                    )
                    connection.commit()

        output = self.root / "late-main-drift-package"
        with patch.object(
            recovery,
            "_online_backup",
            side_effect=backup_then_mutate_earlier_database,
        ):
            with self.assertRaisesRegex(
                RecoveryError, "database_source_provenance_mismatch"
            ):
                recovery._build_package_for_test(
                    phase="final",
                    output=output,
                    _test_project_root=self.project,
                    service_quiescence_digest=QUIESCENCE_DIGEST,
                )
        self.assertFalse(output.exists())
        self.assertEqual(list(self.root.glob(".yunos-v10-package-*")), [])

    def test_canonical_destination_is_used_for_source_overlap(self) -> None:
        lexical_output = self.root / "lexical-alias-package"
        canonical_target = self.assets / "canonical-package"
        parent_identity = recovery._directory_identity(self.assets)
        with patch.object(
            recovery,
            "_safe_new_destination",
            return_value=(canonical_target, self.assets, parent_identity),
        ):
            with self.assertRaisesRegex(
                RecoveryError, "package_destination_overlaps_source"
            ):
                recovery._build_package_for_test(
                    phase="final",
                    output=lexical_output,
                    _test_project_root=self.project,
                    service_quiescence_digest=QUIESCENCE_DIGEST,
                )
        self.assertFalse(canonical_target.exists())
        self.assertFalse(lexical_output.exists())

    def test_package_parent_identity_failure_after_rename_preserves_evidence(self) -> None:
        original = recovery._assert_directory_identity
        calls = 0

        def change_after_rename(
            parent: Path, expected: tuple[int, int]
        ) -> None:
            nonlocal calls
            calls += 1
            original(parent, expected)
            if calls == 2:
                raise RecoveryError("destination_parent_identity_changed")

        output = self.root / "parent-race-package"
        with patch.object(
            recovery,
            "_assert_directory_identity",
            side_effect=change_after_rename,
        ):
            with self.assertRaisesRegex(
                RecoveryError, "destination_parent_identity_changed"
            ):
                recovery._build_package_for_test(
                    phase="final",
                    output=output,
                    _test_project_root=self.project,
                    service_quiescence_digest=QUIESCENCE_DIGEST,
                )
        self.assertEqual(calls, 2)
        self.assertTrue(output.is_dir())
        verify_package(output, expected_phase="final")
        self.assertEqual(list(self.root.glob(".yunos-v10-package-*")), [])

    def test_restore_parent_identity_failure_after_rename_preserves_evidence(self) -> None:
        package, manifest = self._build("restore-parent-race-source")
        original = recovery._assert_directory_identity
        calls = 0

        def change_after_rename(
            parent: Path, expected: tuple[int, int]
        ) -> None:
            nonlocal calls
            calls += 1
            original(parent, expected)
            if calls == 2:
                raise RecoveryError("destination_parent_identity_changed")

        destination = self.root / "parent-race-restore"
        with patch.object(
            recovery,
            "_assert_directory_identity",
            side_effect=change_after_rename,
        ):
            with self.assertRaisesRegex(
                RecoveryError, "destination_parent_identity_changed"
            ):
                restore_package(
                    package=package,
                    destination=destination,
                    port=18779,
                    expected_phase="final",
                )
        self.assertEqual(calls, 2)
        self.assertTrue(destination.is_dir())
        runtime = json.loads(
            (destination / "restore_runtime.json").read_text(encoding="utf-8")
        )
        recovery._validate_sealed_owner_marker(
            destination,
            purpose="restore",
            phase="final",
            manifest_digest=manifest["manifest_digest"],
            backup_set_digest=manifest["backup_set_digest"],
            ownership_identity_digest=runtime[
                "ownership_marker_identity_digest"
            ],
            runtime_digest=runtime["runtime_digest"],
        )
        self.assertEqual(list(self.root.glob(".yunos-v10-restore-*")), [])

    def test_sealed_markers_are_permanent_bound_and_never_unlinked(self) -> None:
        with patch.object(
            Path,
            "unlink",
            side_effect=AssertionError("publication must not unlink marker"),
        ) as unlink:
            package, manifest = self._build("permanent-marker-package")
        unlink.assert_not_called()
        package_marker_path = package / recovery._OWNER_MARKER
        package_marker = json.loads(
            package_marker_path.read_text(encoding="utf-8")
        )
        self.assertEqual(
            recovery._SEALED_MARKER_FIELDS, set(package_marker)
        )
        self.assertEqual("package", package_marker["purpose"])
        self.assertIsNone(package_marker["runtime_digest"])
        self.assertEqual(
            manifest["ownership_marker_identity_digest"],
            package_marker["ownership_identity_digest"],
        )
        verify_package(package, expected_phase="final")

        destination = self.root / "permanent-marker-restore"
        with patch.object(
            Path,
            "unlink",
            side_effect=AssertionError("publication must not unlink marker"),
        ) as unlink:
            report = restore_package(
                package=package,
                destination=destination,
                port=18780,
                expected_phase="final",
            )
        unlink.assert_not_called()
        self.assertEqual("restored", report["status"])
        restore_marker = json.loads(
            (destination / recovery._OWNER_MARKER).read_text(encoding="utf-8")
        )
        runtime = json.loads(
            (destination / "restore_runtime.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            recovery._SEALED_MARKER_FIELDS, set(restore_marker)
        )
        self.assertEqual("restore", restore_marker["purpose"])
        self.assertEqual(
            runtime["runtime_digest"], restore_marker["runtime_digest"]
        )
        self.assertEqual(
            runtime["ownership_marker_identity_digest"],
            restore_marker["ownership_identity_digest"],
        )

        package_marker["purpose"] = "restore"
        package_marker["marker_digest"] = canonical_digest(
            {
                key: value
                for key, value in package_marker.items()
                if key != "marker_digest"
            }
        )
        package_marker_path.write_text(
            canonical_json(package_marker) + "\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(
            RecoveryError, "ownership_marker_binding_mismatch"
        ):
            verify_package(package, expected_phase="final")

    def test_package_target_swap_after_rename_preserves_intruder(self) -> None:
        original = recovery._assert_owned_target_identity
        calls = 0
        displaced = self.root / "displaced-owned-package"

        def swap_on_first_check(
            target: Path, expected: tuple[int, int]
        ) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                target.rename(displaced)
                target.mkdir()
                (target / "intruder.txt").write_text(
                    "must-not-be-cleaned", encoding="utf-8"
                )
            original(target, expected)

        output = self.root / "target-race-package"
        with patch.object(
            recovery,
            "_assert_owned_target_identity",
            side_effect=swap_on_first_check,
        ), self.assertRaisesRegex(
            RecoveryError, "destination_target_identity_changed"
        ):
            recovery._build_package_for_test(
                phase="final",
                output=output,
                _test_project_root=self.project,
                service_quiescence_digest=QUIESCENCE_DIGEST,
            )
        self.assertEqual(1, calls)
        self.assertEqual(
            "must-not-be-cleaned",
            (output / "intruder.txt").read_text(encoding="utf-8"),
        )
        self.assertTrue(displaced.is_dir())

    def test_restore_target_swap_after_rename_preserves_intruder(self) -> None:
        package, _manifest = self._build("restore-target-race-source")
        original = recovery._assert_owned_target_identity
        calls = 0
        displaced = self.root / "displaced-owned-restore"

        def swap_on_first_check(
            target: Path, expected: tuple[int, int]
        ) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                target.rename(displaced)
                target.mkdir()
                (target / "intruder.txt").write_text(
                    "must-not-be-cleaned", encoding="utf-8"
                )
            original(target, expected)

        destination = self.root / "target-race-restore"
        with patch.object(
            recovery,
            "_assert_owned_target_identity",
            side_effect=swap_on_first_check,
        ), self.assertRaisesRegex(
            RecoveryError, "destination_target_identity_changed"
        ):
            restore_package(
                package=package,
                destination=destination,
                port=18779,
                expected_phase="final",
            )
        self.assertEqual(1, calls)
        self.assertEqual(
            "must-not-be-cleaned",
            (destination / "intruder.txt").read_text(encoding="utf-8"),
        )
        self.assertTrue(displaced.is_dir())


if __name__ == "__main__":
    unittest.main()
