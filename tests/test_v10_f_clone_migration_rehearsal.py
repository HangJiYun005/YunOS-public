from __future__ import annotations

from contextlib import closing, redirect_stdout
import hashlib
import inspect
import io
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from memory.operational_state_models import (
    AuthorityArtifact,
    SourceAnchor,
    build_state_event,
    canonical_digest,
    make_identity,
)
from memory.operational_state_store import (
    OperationalStateStore,
    initialize_operational_database,
)
from tools import v10_f_clone_migration_rehearsal as rehearsal
from tools.v10_release_recovery import (
    DEFAULT_ASSET_ROOT,
    DEFAULT_DATABASES,
    _sqlite_file_gates,
    audit_tree,
)


T0 = "2026-08-02T00:00:00.000000Z"
T1 = "2026-08-02T00:01:00.000000Z"
T2 = "2026-08-02T00:02:00.000000Z"
T3 = "2026-08-02T00:03:00.000000Z"
T9 = "2027-08-02T00:00:00.000000Z"
HEAD = "a" * 40


class V10FCloneMigrationRehearsalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temporary.name)
        (self.root / "output").mkdir()
        self.assets = self.root / DEFAULT_ASSET_ROOT
        self.assets.mkdir(parents=True)
        (self.assets / "fixture.txt").write_text(
            "owned fixture asset\n", encoding="utf-8"
        )
        for label, relative in DEFAULT_DATABASES.items():
            database = self.root / relative
            database.parent.mkdir(parents=True, exist_ok=True)
            if label == "operational":
                initialize_operational_database(database, created_at=T0)
                self._seed_project(database)
            else:
                with closing(sqlite3.connect(database)) as connection:
                    connection.execute(
                        "CREATE TABLE fixture_records(id INTEGER PRIMARY KEY, value TEXT)"
                    )
                    connection.execute(
                        "INSERT INTO fixture_records(value) VALUES(?)", (label,)
                    )
                    connection.commit()
        self.git_state = {
            "head": HEAD,
            "tracked_worktree_clean": True,
            "staging_empty": True,
            "untracked_empty": True,
            "worktree_clean": True,
            "stash_ref": "b" * 40,
        }
        self.git_patch = patch.object(
            rehearsal, "_capture_git_state", return_value=self.git_state
        )
        self.git_patch.start()
        self.env_patch = patch.dict(
            os.environ,
            {rehearsal.AUTHORIZATION_ENV: rehearsal.AUTHORIZATION_ENV_VALUE},
            clear=False,
        )
        self.env_patch.start()
        self.confirmation = rehearsal.CloneMigrationRehearsalConfirmation(
            production=False,
            confirm_one_shot=True,
            confirm_readonly_production=True,
            confirm_disposable_clones=True,
            confirm_no_service_stop=True,
            confirm_no_git_or_stash_mutation=True,
            confirm_head=HEAD,
            user_authorization_sha256=rehearsal.USER_AUTHORIZATION_SHA256,
        )

    def tearDown(self) -> None:
        self.env_patch.stop()
        self.git_patch.stop()
        self.temporary.cleanup()

    def _seed_project(self, database: Path) -> None:
        store = OperationalStateStore(database)
        project_id = "project:yunos-clone-rehearsal"
        authority = AuthorityArtifact(
            authority_id="authority:clone-rehearsal-fixture",
            issued_by="actor:local-user",
            issued_at=T0,
            expires_at=T9,
            target_project_id=project_id,
            allowed_event_types=("project_declared",),
            read_scope=("operational-state",),
            write_scope=("*",),
            prohibited_operations=("push", "create-next-node"),
            commit_permission=True,
            merge_permission=False,
            tag_permission=False,
            push_permission=False,
            next_node_permission=False,
        )
        anchor = SourceAnchor(
            anchor_id="anchor:clone-rehearsal-fixture",
            source_kind="user_declaration",
            locator="fixture:clone-rehearsal",
            evidence_digest=hashlib.sha256(b"clone-rehearsal").hexdigest(),
            observed_at=T0,
            privacy_scope="private_metadata",
        )
        owner = "owner:clone-rehearsal-fixture"
        token = "clone-rehearsal-fixture-token"
        store.acquire_writer_lease(
            owner_id=owner,
            token=token,
            acquired_at=T0,
            expires_at=T9,
        )
        event = build_state_event(
            project_id=project_id,
            entity_id=project_id,
            entity_type="project",
            event_type="project_declared",
            source_kind="declared",
            source_anchor=anchor,
            authority_kind="user",
            actor_kind="user",
            actor_id="actor:local-user",
            observed_at=T1,
            effective_at=T1,
            recorded_at=T1,
            expires_at=None,
            supersedes_event_id=None,
            caused_by_event_id=None,
            idempotency_key=make_identity(
                "idempotency", {"project": project_id, "event": T1}
            ),
            conflict_group_id=None,
            payload={
                "state": "active",
                "name": "YunOS clone rehearsal",
                "focus": True,
                "priority": 1,
                "current_version": "v9.0.1",
            },
            privacy_scope="private_metadata",
            authority_digest=authority.authority_digest,
        )
        preview = store.preview_event(authority=authority, event=event)
        store.commit_preview(
            authority=authority,
            preview=preview,
            lease_owner_id=owner,
            lease_token=token,
        )
        store.release_writer_lease(
            owner_id=owner,
            token=token,
            released_at=T2,
        )

    def _source_boundary(self) -> dict:
        databases = {
            label: _sqlite_file_gates(self.root / relative)
            for label, relative in sorted(DEFAULT_DATABASES.items())
        }
        assets = audit_tree(self.assets)
        return {"databases": databases, "assets": assets}

    def _run(self) -> dict:
        return rehearsal._run_clone_migration_rehearsal_for_test(
            _test_project_root=self.root,
            confirmation=self.confirmation,
        )

    def test_real_clone_migration_faults_restore_and_source_boundaries(self) -> None:
        source_before = self._source_boundary()
        apply_calls = 0
        original_apply = rehearsal._apply_fixed_cognitive_ddl

        def audited_apply(*args, **kwargs):
            nonlocal apply_calls
            apply_calls += 1
            return original_apply(*args, **kwargs)

        with patch.object(
            rehearsal,
            "_apply_fixed_cognitive_ddl",
            side_effect=audited_apply,
        ):
            report = self._run()

        self.assertEqual("clone_migration_rehearsal_go", report["status"])
        self.assertEqual(3, apply_calls)
        self.assertEqual(9001, report["migration"]["source_core_schema_version"])
        self.assertEqual(
            10001,
            report["migration"]["target_cognitive_schema_generation"],
        )
        self.assertEqual(
            45, report["migration"]["expected_cognitive_object_count"]
        )
        success = report["success"]["migration"]
        self.assertEqual("completed", success["reconciliation_state"])
        self.assertEqual(1, success["ddl_apply_count"])
        self.assertEqual(
            45, success["verification"]["cognitive_object_count"]
        )
        self.assertEqual("ok", success["verification"]["integrity"])
        self.assertEqual(0, success["verification"]["foreign_key_violations"])
        self.assertEqual(0, success["verification"]["writer_lease_count"])

        precommit = report["fault_injection"]["precommit"]["result"]
        self.assertEqual("rolled_back", precommit["reconciliation_state"])
        self.assertFalse(precommit["commit_reached"])
        self.assertEqual(1, precommit["ddl_apply_count"])
        self.assertFalse(precommit["reconcile_replayed_ddl"])
        aftercommit = report["fault_injection"]["aftercommit"]["result"]
        self.assertEqual("completed", aftercommit["reconciliation_state"])
        self.assertTrue(aftercommit["commit_reached"])
        self.assertEqual(1, aftercommit["ddl_apply_count"])
        self.assertFalse(aftercommit["reconcile_replayed_ddl"])

        self.assertTrue(report["restore"]["bound_backup_revalidated"])
        self.assertEqual(
            "no_reverse_ddl_restore_bound_backup_to_new_owned_clone",
            report["restore"]["rollback_policy"],
        )
        self.assertTrue(report["restore"]["aftercommit_clone_retained"])
        self.assertEqual(
            9001,
            report["restore"]["source_schema_restored"]["schema_version"],
        )
        self.assertEqual(
            0,
            report["restore"]["source_schema_restored"][
                "cognitive_object_count"
            ],
        )
        self.assertTrue(all(report["comparisons"].values()))
        for context in (
            report["baseline"]["context_summary"],
            report["success"]["context_summary"],
            report["fault_injection"]["precommit"]["context_summary"],
            report["fault_injection"]["aftercommit"]["context_summary"],
            report["restore"]["context_summary"],
        ):
            self.assertEqual(
                "sha256_bound_owned_temp_mirror",
                context["evaluation_boundary"],
            )
            self.assertTrue(context["ephemeral_mirror_cleaned"])
        self.assertTrue(
            report["production_boundary"]["other_four_databases_unchanged"]
        )
        self.assertTrue(
            report["production_boundary"]["operational_database_unchanged"]
        )
        self.assertTrue(report["production_boundary"]["assets_unchanged"])
        self.assertEqual(source_before, self._source_boundary())
        self.assertFalse(report["production_write_performed"])
        self.assertFalse(report["service_stop_performed"])
        self.assertFalse(report["git_mutation_performed"])
        self.assertFalse(report["stash_mutation_performed"])

        output = self.root / "output" / rehearsal.OUTPUT_NAME
        self.assertTrue(output.is_dir())
        for name in (
            rehearsal.SOURCE_SNAPSHOT_NAME,
            rehearsal.SUCCESS_CLONE_NAME,
            rehearsal.PRECOMMIT_CLONE_NAME,
            rehearsal.AFTERCOMMIT_CLONE_NAME,
            rehearsal.RESTORE_CLONE_NAME,
            rehearsal.REPORT_NAME,
            rehearsal.OWNER_MARKER_NAME,
        ):
            self.assertTrue((output / name).is_file(), name)
        persisted = json.loads(
            (output / rehearsal.REPORT_NAME).read_text(encoding="utf-8")
        )
        self.assertEqual(
            persisted["report_digest"],
            canonical_digest(
                {
                    key: value
                    for key, value in persisted.items()
                    if key != "report_digest"
                }
            ),
        )
        marker = json.loads(
            (output / rehearsal.OWNER_MARKER_NAME).read_text(encoding="utf-8")
        )
        self.assertEqual("sealed", marker["state"])
        self.assertEqual(report["report_digest"], marker["report_digest"])
        self.assertEqual(
            marker["marker_digest"],
            canonical_digest(
                {
                    key: value
                    for key, value in marker.items()
                    if key != "marker_digest"
                }
            ),
        )

    def test_production_shaped_source_is_opened_only_immutable_readonly(self) -> None:
        source = (self.root / DEFAULT_DATABASES["operational"]).resolve()
        source_uri = source.as_uri()
        original_connect = sqlite3.connect
        source_calls: list[tuple[str, dict]] = []

        def audited_connect(database, *args, **kwargs):
            if isinstance(database, str) and database.startswith(source_uri):
                source_calls.append((database, dict(kwargs)))
            return original_connect(database, *args, **kwargs)

        with patch.object(
            rehearsal.sqlite3, "connect", side_effect=audited_connect
        ):
            report = self._run()
        self.assertEqual("clone_migration_rehearsal_go", report["status"])
        self.assertEqual(1, len(source_calls))
        uri, kwargs = source_calls[0]
        self.assertTrue(uri.endswith("?mode=ro&immutable=1"))
        self.assertIs(kwargs.get("uri"), True)
        self.assertEqual(
            "readonly_immutable_query_only", report["source_open_mode"]
        )

    def test_one_shot_sentry_blocks_replay_before_any_source_capture(self) -> None:
        self._run()
        with patch.object(
            rehearsal,
            "_capture_data_boundary",
            side_effect=AssertionError("source capture must not run"),
        ):
            with self.assertRaisesRegex(
                rehearsal.CloneMigrationRehearsalError,
                "one_shot_artifact_exists",
            ):
                self._run()
        self.assertTrue(
            (self.root / "output" / rehearsal.SENTRY_NAME).is_file()
        )

    def test_failure_cleans_only_owned_staging_and_records_sentry(self) -> None:
        source_before = self._source_boundary()
        with patch.object(
            rehearsal,
            "_apply_once",
            side_effect=rehearsal.CloneMigrationRehearsalError(
                "forced_clone_failure"
            ),
        ):
            with self.assertRaisesRegex(
                rehearsal.CloneMigrationRehearsalError,
                "forced_clone_failure",
            ):
                self._run()
        output_parent = self.root / "output"
        self.assertFalse((output_parent / rehearsal.OUTPUT_NAME).exists())
        self.assertEqual(
            [], list(output_parent.glob(".v10-f-clone-rehearsal-*"))
        )
        self.assertTrue((output_parent / rehearsal.SENTRY_NAME).is_file())
        failure = json.loads(
            (output_parent / rehearsal.FAILURE_NAME).read_text(encoding="utf-8")
        )
        self.assertEqual("forced_clone_failure", failure["reason_code"])
        self.assertFalse(failure["fixed_output_retained"])
        self.assertEqual("owned_staging_only", failure["staging_cleanup_policy"])
        self.assertEqual(source_before, self._source_boundary())

    def test_post_rename_validation_failure_retains_final_evidence(self) -> None:
        original = rehearsal._validate_owned_directory
        sealed_calls = 0

        def fail_final_validation(path, nonce, identity, *, sealed_digest):
            nonlocal sealed_calls
            original(
                path,
                nonce,
                identity,
                sealed_digest=sealed_digest,
            )
            if sealed_digest is not None:
                sealed_calls += 1
                if sealed_calls == 2:
                    raise rehearsal.CloneMigrationRehearsalError(
                        "post_rename_validation_fault"
                    )

        with patch.object(
            rehearsal,
            "_validate_owned_directory",
            side_effect=fail_final_validation,
        ):
            with self.assertRaisesRegex(
                rehearsal.CloneMigrationRehearsalError,
                "post_rename_validation_fault",
            ):
                self._run()
        output_parent = self.root / "output"
        final_output = output_parent / rehearsal.OUTPUT_NAME
        self.assertEqual(2, sealed_calls)
        self.assertTrue(final_output.is_dir())
        self.assertTrue((final_output / rehearsal.REPORT_NAME).is_file())
        self.assertTrue((final_output / rehearsal.OWNER_MARKER_NAME).is_file())
        self.assertEqual(
            [], list(output_parent.glob(".v10-f-clone-rehearsal-*"))
        )
        failure = json.loads(
            (output_parent / rehearsal.FAILURE_NAME).read_text(encoding="utf-8")
        )
        self.assertTrue(failure["fixed_output_retained"])
        self.assertEqual(
            "post_rename_validation_fault", failure["reason_code"]
        )

    def test_authority_environment_and_confirmation_fail_before_sentry(self) -> None:
        output_parent = self.root / "output"
        self.env_patch.stop()
        try:
            with self.assertRaisesRegex(
                rehearsal.CloneMigrationRehearsalError,
                "authorization_environment_gate_closed",
            ):
                self._run()
        finally:
            self.env_patch.start()
        self.assertFalse((output_parent / rehearsal.SENTRY_NAME).exists())

        wrong = rehearsal.CloneMigrationRehearsalConfirmation(
            production=False,
            confirm_one_shot=True,
            confirm_readonly_production=True,
            confirm_disposable_clones=True,
            confirm_no_service_stop=True,
            confirm_no_git_or_stash_mutation=True,
            confirm_head=HEAD,
            user_authorization_sha256="f" * 64,
        )
        with self.assertRaisesRegex(
            rehearsal.CloneMigrationRehearsalError,
            "user_authorization_digest_mismatch",
        ):
            rehearsal._run_clone_migration_rehearsal_for_test(
                _test_project_root=self.root,
                confirmation=wrong,
            )
        self.assertFalse((output_parent / rehearsal.SENTRY_NAME).exists())

    def test_public_and_cli_surfaces_have_no_path_database_or_di_override(self) -> None:
        signature = inspect.signature(
            rehearsal.run_production_clone_migration_rehearsal
        )
        self.assertEqual(["confirmation"], list(signature.parameters))
        self.assertEqual(
            inspect.Parameter.KEYWORD_ONLY,
            signature.parameters["confirmation"].kind,
        )
        self.assertFalse(hasattr(rehearsal, "_install_production_api"))
        production_confirmation = rehearsal.CloneMigrationRehearsalConfirmation(
            production=True,
            confirm_one_shot=True,
            confirm_readonly_production=True,
            confirm_disposable_clones=True,
            confirm_no_service_stop=True,
            confirm_no_git_or_stash_mutation=True,
            confirm_head=HEAD,
            user_authorization_sha256=rehearsal.USER_AUTHORIZATION_SHA256,
        )
        for unexpected in (
            {"database": self.root / "fake.db"},
            {"output": self.root / "fake-output"},
            {"_test_project_root": self.root},
            {"executor": lambda: None},
        ):
            with self.assertRaises(TypeError):
                rehearsal.run_production_clone_migration_rehearsal(
                    confirmation=production_confirmation,
                    **unexpected,
                )
        with patch.dict(
            os.environ, {rehearsal.AUTHORIZATION_ENV: ""}
        ), patch.object(
            rehearsal,
            "_run_bound_rehearsal",
            side_effect=AssertionError("global executor replacement"),
        ):
            with self.assertRaisesRegex(
                rehearsal.CloneMigrationRehearsalError,
                "authorization_environment_gate_closed",
            ):
                rehearsal.run_production_clone_migration_rehearsal(
                    confirmation=production_confirmation
                )
        with self.assertRaisesRegex(
            rehearsal.CloneMigrationRehearsalError,
            "production_root_not_fixed",
        ):
            rehearsal._run_bound_rehearsal(
                self.root,
                confirmation=production_confirmation,
                production=True,
            )
        with self.assertRaisesRegex(
            rehearsal.CloneMigrationRehearsalError,
            "test_root_not_disposable",
        ):
            rehearsal._run_clone_migration_rehearsal_for_test(
                _test_project_root=Path(rehearsal.__file__).resolve().parents[1],
                confirmation=self.confirmation,
            )

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(0, rehearsal.main([]))
        preview = json.loads(stdout.getvalue())
        self.assertEqual("dry_run", preview["mode"])
        self.assertFalse(preview["path_override_supported"])
        self.assertFalse(preview["database_override_supported"])
        self.assertFalse(preview["dependency_injection_supported"])

        destinations = {action.dest for action in rehearsal._parser()._actions}
        self.assertFalse(
            destinations
            & {
                "path",
                "project_root",
                "database",
                "operational_database",
                "output",
                "sentry",
                "executor",
            }
        )
        self.assertEqual(
            {
                ("rev-parse", "HEAD"),
                ("status", "--porcelain=v1", "--untracked-files=all"),
                ("rev-parse", "-q", "--verify", "refs/stash"),
            },
            set(rehearsal._GIT_ALLOWLIST),
        )
        source = Path(rehearsal.__file__).read_text(encoding="utf-8")
        self.assertNotIn("Stop-Service", source)
        self.assertNotIn("Stop-Process", source)
        self.assertNotIn("git stash", source)
        self.assertNotIn("git reset", source)
        self.assertNotIn("git checkout", source)
        self.assertNotIn("shell=True", source)

    def test_execute_cli_with_closed_env_refuses_before_production_access(self) -> None:
        arguments = [
            "--execute",
            "--confirm-one-shot",
            "--confirm-readonly-production",
            "--confirm-disposable-clones",
            "--confirm-no-service-stop",
            "--confirm-no-git-or-stash-mutation",
            "--confirm-head",
            HEAD,
            "--confirm-user-authorization-sha256",
            rehearsal.USER_AUTHORIZATION_SHA256,
        ]
        stdout = io.StringIO()
        with patch.dict(os.environ, {rehearsal.AUTHORIZATION_ENV: ""}), patch.object(
            rehearsal,
            "_bound_paths",
            side_effect=AssertionError("production paths must not be touched"),
        ), redirect_stdout(stdout):
            self.assertEqual(1, rehearsal.main(arguments))
        result = json.loads(stdout.getvalue())
        self.assertEqual("refused", result["status"])
        self.assertEqual(
            "authorization_environment_gate_closed", result["reason_code"]
        )


if __name__ == "__main__":
    unittest.main()
