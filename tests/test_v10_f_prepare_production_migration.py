from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import inspect
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from memory.cognitive_production_migration import (
    _execute_bound_cognitive_migration,
    _paths_for_clone_test,
    build_cognitive_migration_plan_for_clone,
)
from memory.operational_state_store import (
    OperationalStateStore,
    initialize_operational_database,
)
from tests.v10_f_production_migration_fixtures import (
    clone_live_gate_probe,
)
from tools.v10_f_prepare_production_migration import (
    CAPTURE_CONTROLLER_DIGEST,
    FORMAL_CAPTURE_ROOT,
    PRODUCTION_AUTHORITY_RECORD,
    PRODUCTION_BACKUP_VERIFICATION_RECORD,
    PRODUCTION_MIGRATION_CONTROL_ROOT,
    ControlPreparationError,
    USER_AUTHORIZATION_TEXT_SHA256,
    _FIXED_CAPTURE_ARTIFACT_VALIDATOR,
    _FIXED_PRODUCTION_PREPARE_EXECUTOR,
    _safe_new_control_root,
    _write_exclusive,
    build_sealed_quiescence_capture_artifact,
    _git_head_state,
    build_parser,
    build_service_quiescence_evidence,
    prepare_fixed_production_controls,
    prepare_control_records,
    validate_sealed_quiescence_capture_artifact,
)
from tools.v10_release_recovery import (
    DEFAULT_ASSET_ROOT,
    DEFAULT_DATABASES,
    DEFAULT_RELEASE_REPORT_ROOT,
    _build_package_for_test,
)


T0 = "2026-08-01T01:00:00.000000Z"
T1 = "2026-08-01T01:01:00.000000Z"
T2 = "2026-08-01T01:31:00.000000Z"


class V10FPrepareProductionMigrationTest(unittest.TestCase):
    @staticmethod
    def source_identities() -> dict[str, object]:
        records: dict[str, object] = {}
        for index, source_id in enumerate(
            (
                "capture_controller",
                "operational_database",
                "tailscale_executable",
            ),
            start=1,
        ):
            external = source_id == "tailscale_executable"
            records[source_id] = {
                "source_id": source_id,
                "path_scope": (
                    "external-path-digest" if external else "project"
                ),
                "path_reference": (
                    "e" * 64
                    if external
                    else (
                        "tools/v10_f_capture_production_quiescence.py"
                        if source_id == "capture_controller"
                        else "data/operational/ayunos_operational_state.db"
                    )
                ),
                "device": index,
                "inode": index + 10,
                "size": index + 100,
                "mtime_ns": index + 1000,
                "sha256": f"{index:x}" * 64,
            }
        return records

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        for label, relative in DEFAULT_DATABASES.items():
            target = self.project / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if label == "operational":
                initialize_operational_database(target, created_at=T0)
            else:
                with closing(sqlite3.connect(target)) as connection:
                    connection.execute(
                        "CREATE TABLE records(id INTEGER PRIMARY KEY,value TEXT)"
                    )
                    connection.execute(
                        "INSERT INTO records(value) VALUES(?)",
                        (label,),
                    )
                    connection.commit()
        OperationalStateStore(
            self.project / DEFAULT_DATABASES["operational"]
        ).create_snapshot(created_at=T0)
        assets = self.project / DEFAULT_ASSET_ROOT
        assets.mkdir(parents=True)
        (assets / "evidence.txt").write_text("bounded\n", encoding="utf-8")
        reports = self.project / DEFAULT_RELEASE_REPORT_ROOT
        reports.mkdir(parents=True)
        (reports / "yunos_v10_f_preproduction.md").write_text(
            "# v10-F preproduction\n",
            encoding="utf-8",
        )
        self.evidence = build_service_quiescence_evidence(
            captured_at=T0,
            tailscale_serve_config_digest="d" * 64,
            local_service_listener_count=0,
            writer_lease_count=0,
            operational_wal_size=0,
            production_write_env_enabled=False,
            tailscale_backend="Running",
            funnel_enabled=False,
        )
        self.capture_artifact = build_sealed_quiescence_capture_artifact(
            evidence=self.evidence,
            source_identities=self.source_identities(),
            nonce="1" * 32,
        )
        self.package = self.root / "preproduction-package"
        _build_package_for_test(
            phase="preproduction",
            output=self.package,
            _test_project_root=self.project,
            rehearsal_port=18841,
            service_quiescence_digest=self.evidence["evidence_digest"],
        )
        self.operational = self.project / DEFAULT_DATABASES["operational"]
        self.plan = build_cognitive_migration_plan_for_clone(self.operational)

    def prepare(self, control: Path | None = None, **options):
        root = control or (self.root / "controls")
        return prepare_control_records(
            preproduction_package=self.package,
            quiescence_capture_artifact=self.capture_artifact,
            plan=self.plan,
            control_root=root,
            authority_record_path=root / "cognitive_migration_authority.json",
            backup_record_path=(
                root / "cognitive_migration_backup_verification.json"
            ),
            code_commit="a" * 40,
            authorized_at=T1,
            expires_at=T2,
            **options,
        )

    def test_verified_package_prepares_fixed_records_and_executes_clone_once(self) -> None:
        manifest = self.prepare()
        control = self.root / "controls"
        self.assertEqual(
            {
                "cognitive_migration_plan.json",
                "cognitive_migration_backup_verification.json",
                "cognitive_migration_authority.json",
                "service_quiescence_evidence.json",
                "control_manifest.json",
            },
            {item.name for item in control.iterdir()},
        )
        self.assertFalse(manifest["migration_executed"])
        self.assertEqual("a" * 40, manifest["code_commit"])
        authority = json.loads(
            (control / "cognitive_migration_authority.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            USER_AUTHORIZATION_TEXT_SHA256,
            authority["user_authorization_text_digest"],
        )
        backup = json.loads(
            (control / "cognitive_migration_backup_verification.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(str(self.package), backup["preproduction_package_absolute_path"])
        self.assertEqual(
            "databases/ayunos_operational_state.db",
            backup["operational_backup_relative_path"],
        )
        self.assertTrue(backup["release_manifest_file_gate"]["exists"])
        self.assertEqual(
            self.evidence["captured_at"],
            authority["service_quiescence_captured_at"],
        )
        paths = _paths_for_clone_test(self.operational, control)
        with patch(
            "memory.cognitive_production_migration._capture_code_state",
            return_value={
                "commit": "a" * 40,
                "tracked_worktree_clean": True,
                "staging_empty": True,
                "untracked_empty": True,
                "worktree_clean": True,
            },
        ):
            result = _execute_bound_cognitive_migration(
                paths,
                current_time=datetime(
                    2026, 8, 1, 1, 2, tzinfo=timezone.utc
                ),
                _test_live_gate_probe=clone_live_gate_probe,
                _test_tailscale_gate_probe=lambda: {
                    "tailscale_backend": "Running",
                    "tailscale_serve_config_digest": "d" * 64,
                    "funnel_enabled": False,
                },
            )
        self.assertEqual("completed", result["status"])
        self.assertTrue(result["system_verified"])
        self.assertEqual("waiting_acceptance", result["acceptance_state"])

    def test_stale_or_future_quiescence_never_creates_controls(self) -> None:
        cases = (
            ("stale", "2026-08-01T00:58:59.000000Z"),
            ("future", "2026-08-01T01:01:01.000000Z"),
        )
        for name, captured_at in cases:
            with self.subTest(name=name):
                evidence = build_service_quiescence_evidence(
                    captured_at=captured_at,
                    tailscale_serve_config_digest="d" * 64,
                    local_service_listener_count=0,
                    writer_lease_count=0,
                    operational_wal_size=0,
                    production_write_env_enabled=False,
                    tailscale_backend="Running",
                    funnel_enabled=False,
                )
                artifact = build_sealed_quiescence_capture_artifact(
                    evidence=evidence,
                    source_identities=self.source_identities(),
                    nonce=("2" if name == "stale" else "3") * 32,
                )
                control = self.root / f"{name}-controls"
                with self.assertRaisesRegex(
                    ControlPreparationError,
                    "service_quiescence_authority_mismatch",
                ):
                    prepare_control_records(
                        preproduction_package=self.package,
                        quiescence_capture_artifact=artifact,
                        plan=self.plan,
                        control_root=control,
                        authority_record_path=(
                            control / "cognitive_migration_authority.json"
                        ),
                        backup_record_path=(
                            control
                            / "cognitive_migration_backup_verification.json"
                        ),
                        code_commit="a" * 40,
                        authorized_at=T1,
                        expires_at=T2,
                    )
                self.assertFalse(control.exists())

    def test_quiescence_or_existing_root_mismatch_fails_before_records(self) -> None:
        altered = dict(self.evidence)
        altered["writer_lease_count"] = 1
        with self.assertRaisesRegex(
            ControlPreparationError,
            "capture_artifact_invalid",
        ):
            prepare_control_records(
                preproduction_package=self.package,
                quiescence_capture_artifact=altered,
                plan=self.plan,
                control_root=self.root / "bad-controls",
                authority_record_path=(
                    self.root / "bad-controls/cognitive_migration_authority.json"
                ),
                backup_record_path=(
                    self.root
                    / "bad-controls/cognitive_migration_backup_verification.json"
                ),
                code_commit="a" * 40,
                authorized_at=T1,
                expires_at=T2,
            )
        self.assertFalse((self.root / "bad-controls").exists())

        existing = self.root / "existing-controls"
        existing.mkdir()
        with self.assertRaisesRegex(
            ControlPreparationError,
            "production_control_root_exists",
        ):
            self.prepare(existing)
        self.assertEqual([], list(existing.iterdir()))

    def test_cli_confirms_user_digest_and_exposes_no_database_or_sql_override(self) -> None:
        parser = build_parser()
        options = {action.dest for action in parser._actions}
        self.assertIn("confirm_user_authorization_sha256", options)
        self.assertNotIn("production_db", options)
        self.assertNotIn("sql", options)
        self.assertNotIn("force", options)
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "--prepare",
                    "--preproduction-package",
                    str(self.package),
                    "--quiescence-evidence",
                    str(self.root / "evidence.json"),
                    "--confirm-head",
                    "a" * 40,
                    "--confirm-user-authorization-sha256",
                    "f" * 64,
                ]
            )

    def test_fixed_production_provenance_rejects_test_fixture_package(
        self,
    ) -> None:
        control = self.root / "mixed-source-controls"
        with self.assertRaisesRegex(
            ControlPreparationError,
            "fixed_production_source_binding_required",
        ):
            self.prepare(
                control,
                require_fixed_production_provenance=True,
            )
        self.assertFalse(control.exists())

    def test_shared_builder_and_unbound_writers_reject_formal_aliases_first(
        self,
    ) -> None:
        roots = (
            PRODUCTION_MIGRATION_CONTROL_ROOT,
            PRODUCTION_MIGRATION_CONTROL_ROOT.parent
            / "."
            / PRODUCTION_MIGRATION_CONTROL_ROOT.name,
            PRODUCTION_MIGRATION_CONTROL_ROOT
            / ".."
            / PRODUCTION_MIGRATION_CONTROL_ROOT.name,
            FORMAL_CAPTURE_ROOT,
        )
        for root in roots:
            with self.subTest(root=str(root)), patch(
                "tools.v10_f_prepare_production_migration.verify_package",
                side_effect=AssertionError("package must not be read"),
            ), self.assertRaisesRegex(
                ControlPreparationError,
                "shared_control_formal_path_forbidden",
            ):
                prepare_control_records(
                    preproduction_package=self.package,
                    quiescence_capture_artifact=self.capture_artifact,
                    plan=self.plan,
                    control_root=root,
                    authority_record_path=(
                        root / "cognitive_migration_authority.json"
                    ),
                    backup_record_path=(
                        root
                        / "cognitive_migration_backup_verification.json"
                    ),
                    code_commit="a" * 40,
                    authorized_at=T1,
                    expires_at=T2,
                    code_state_probe=lambda: (_ for _ in ()).throw(
                        AssertionError("probe must not run")
                    ),
                    expected_code_state={},
                )

        with self.assertRaisesRegex(
            ControlPreparationError, "shared_control_formal_path_forbidden"
        ):
            _safe_new_control_root(PRODUCTION_MIGRATION_CONTROL_ROOT)
        for record in (
            PRODUCTION_AUTHORITY_RECORD,
            PRODUCTION_BACKUP_VERIFICATION_RECORD,
            PRODUCTION_MIGRATION_CONTROL_ROOT
            / "service_quiescence_evidence.json",
        ):
            with self.assertRaisesRegex(
                ControlPreparationError,
                "shared_control_formal_path_forbidden",
            ):
                _write_exclusive(record, {"forged": True})

    def test_shared_builder_rejects_record_escape_reparse_and_hardlink(
        self,
    ) -> None:
        root = self.root / "escape-controls"
        with patch(
            "tools.v10_f_prepare_production_migration.verify_package",
            side_effect=AssertionError("package must not be read"),
        ), self.assertRaisesRegex(
            ControlPreparationError, "production_control_fixed_path_mismatch"
        ):
            prepare_control_records(
                preproduction_package=self.package,
                quiescence_capture_artifact=self.capture_artifact,
                plan=self.plan,
                control_root=root,
                authority_record_path=self.root / "escaped-authority.json",
                backup_record_path=(
                    root / "cognitive_migration_backup_verification.json"
                ),
                code_commit="a" * 40,
                authorized_at=T1,
                expires_at=T2,
            )

        real_parent = self.root / "real-parent"
        real_parent.mkdir()
        alias = self.root / "reparse-parent"
        try:
            alias.symlink_to(real_parent, target_is_directory=True)
        except OSError:
            alias = None
        if alias is not None:
            with self.assertRaisesRegex(
                ControlPreparationError,
                "shared_control_formal_path_forbidden",
            ):
                _safe_new_control_root(alias / "controls")

        original = self.root / "original.json"
        original.write_text("{}\n", encoding="utf-8")
        linked = self.root / "linked.json"
        os.link(original, linked)
        self.assertGreaterEqual(original.stat().st_nlink, 2)
        with self.assertRaisesRegex(
            ControlPreparationError, "production_control_record_exists"
        ):
            _write_exclusive(linked, {"forged": True})

    def test_sealed_capture_rejects_fake_issuer_and_digest_rewrites(self) -> None:
        artifact = build_sealed_quiescence_capture_artifact(
            evidence=self.evidence,
            source_identities=self.source_identities(),
            nonce="1" * 32,
        )
        self.assertEqual(
            CAPTURE_CONTROLLER_DIGEST,
            artifact["issuer"]["controller_digest"],
        )
        self.assertEqual(
            USER_AUTHORIZATION_TEXT_SHA256,
            artifact["issuer"]["user_authorization_text_digest"],
        )
        self.assertEqual(
            artifact,
            validate_sealed_quiescence_capture_artifact(artifact),
        )
        for field, forged in (
            ("issuer_id", "controller:forged"),
            ("nonce", "not-a-nonce"),
            ("capture_digest", "f" * 64),
            ("controller_digest", "f" * 64),
            ("user_authorization_text_digest", "f" * 64),
            ("captured_at", T1),
            ("artifact_digest", "f" * 64),
            ("issuer_digest", "f" * 64),
        ):
            forged_artifact = json.loads(json.dumps(artifact))
            forged_artifact["issuer"][field] = forged
            with self.subTest(field=field), self.assertRaises(
                ControlPreparationError
            ):
                validate_sealed_quiescence_capture_artifact(forged_artifact)
        with self.assertRaisesRegex(
            ControlPreparationError, "capture_artifact_invalid"
        ):
            validate_sealed_quiescence_capture_artifact(self.evidence)

    def test_fixed_prepare_has_no_root_plan_probe_or_clock_injection(self) -> None:
        validator_signature = inspect.signature(
            _FIXED_CAPTURE_ARTIFACT_VALIDATOR
        )
        self.assertEqual(["path"], list(validator_signature.parameters))
        validator_closure = inspect.getclosurevars(
            _FIXED_CAPTURE_ARTIFACT_VALIDATOR
        ).nonlocals
        bound_root = validator_closure["fixed_capture_root"]
        bound_source_probe = validator_closure["real_source_identities"]
        bound_validator_clock = validator_closure["real_clock"]
        with patch(
            "tools.v10_f_prepare_production_migration.FORMAL_CAPTURE_ROOT",
            self.root,
        ), patch(
            "tools.v10_f_prepare_production_migration."
            "capture_fixed_source_identities",
            return_value=self.source_identities(),
        ), patch(
            "tools.v10_f_prepare_production_migration.datetime"
        ):
            patched_validator_closure = inspect.getclosurevars(
                _FIXED_CAPTURE_ARTIFACT_VALIDATOR
            ).nonlocals
            self.assertEqual(
                bound_root,
                patched_validator_closure["fixed_capture_root"],
            )
            self.assertIs(
                bound_source_probe,
                patched_validator_closure["real_source_identities"],
            )
            self.assertIs(
                bound_validator_clock,
                patched_validator_closure["real_clock"],
            )
            with self.assertRaisesRegex(
                ControlPreparationError,
                "service_quiescence_evidence_path_forbidden",
            ):
                _FIXED_CAPTURE_ARTIFACT_VALIDATOR(self.root / "forged.json")

        signature = inspect.signature(prepare_fixed_production_controls)
        self.assertEqual(
            {
                "preproduction_package",
                "quiescence_evidence_path",
                "confirm_head",
                "confirm_user_authorization_sha256",
            },
            set(signature.parameters),
        )
        self.assertNotIn("current_time", signature.parameters)
        self.assertNotIn("plan", signature.parameters)
        self.assertNotIn("control_root", signature.parameters)
        self.assertNotIn("code_state_probe", signature.parameters)
        with self.assertRaises(TypeError):
            prepare_fixed_production_controls(
                preproduction_package=self.package,
                quiescence_evidence_path=self.root / "forged.json",
                confirm_head="a" * 40,
                confirm_user_authorization_sha256=(
                    USER_AUTHORIZATION_TEXT_SHA256
                ),
                current_time=datetime(  # type: ignore[call-arg]
                    2026, 8, 1, 1, 1, tzinfo=timezone.utc
                ),
            )
        executor_type = type(_FIXED_PRODUCTION_PREPARE_EXECUTOR)
        closure = inspect.getclosurevars(executor_type.run).nonlocals
        self.assertTrue(
            {
                "evidence",
                "plan",
                "current_time",
                "control_root",
                "code_state_probe",
            }.isdisjoint(closure)
        )
        source = inspect.getsource(executor_type.run)
        self.assertNotIn("prepare_control_records", source)
        self.assertNotIn("current_time", source)
        bound_plan = closure["real_inspect_plan"]
        bound_capture = closure["real_validate_capture"]
        bound_clock = closure["real_clock"]
        with patch(
            "tools.v10_f_prepare_production_migration."
            "inspect_fixed_production_cognitive_migration_plan",
            return_value={"forged": True},
        ), patch(
            "tools.v10_f_prepare_production_migration."
            "validate_fixed_capture_artifact",
            return_value={"forged": True},
        ), patch(
            "tools.v10_f_prepare_production_migration.datetime"
        ):
            patched_closure = inspect.getclosurevars(executor_type.run).nonlocals
            self.assertIs(bound_plan, patched_closure["real_inspect_plan"])
            self.assertIs(bound_capture, patched_closure["real_validate_capture"])
            self.assertIs(bound_clock, patched_closure["real_clock"])
        with self.assertRaisesRegex(
            ControlPreparationError, "exact_production_prepare_executor_required"
        ):
            executor_type.run(
                object(),
                preproduction_package=self.package,
                quiescence_evidence_path=self.root / "forged.json",
                confirm_head="a" * 40,
                confirm_user_authorization_sha256=(
                    USER_AUTHORIZATION_TEXT_SHA256
                ),
            )

    def test_git_gate_rejects_untracked_files(self) -> None:
        outputs = (
            "",
            "untracked_runtime_code.py\n",
            "a" * 40 + "\n",
            "",
        )
        with patch(
            "tools.v10_f_prepare_production_migration.subprocess.run",
            side_effect=[SimpleNamespace(stdout=value) for value in outputs],
        ) as mocked:
            state = _git_head_state(self.root / "git-gate")
        self.assertEqual(4, mocked.call_count)
        self.assertTrue(state["tracked_worktree_clean"])
        self.assertTrue(state["staging_empty"])
        self.assertFalse(state["untracked_empty"])
        self.assertFalse(state["worktree_clean"])


if __name__ == "__main__":
    unittest.main()
