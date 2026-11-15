from __future__ import annotations

import ast
from contextlib import closing, redirect_stderr, redirect_stdout
from dataclasses import fields, replace
from datetime import datetime, timezone
import hashlib
import inspect
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import textwrap
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from memory.cognitive_objective import ObjectiveIntentError
from memory.cognitive_planning import PlanningError
from memory.cognitive_provider import build_suggested_provider_registry
from memory.cognitive_provider_store import CognitiveProviderStore
from memory.cognitive_state_migration import apply_cognitive_state_migration
from memory.operational_state_context import OperationalStateContextService
from memory.operational_state_models import AuthorityArtifact, OperationalStateError
from memory.operational_state_store import OperationalStateStore
from memory.runtime_enablement import initialize_runtime_enablement_sidecar
from tools import v10_f_real_plan_prepare_acceptance as acceptance
from tools.v9_production_adoption import apply_adoption


ADOPTION_TIME = "2026-07-31T02:00:00.000000Z"
MIGRATION_TIME = "2026-08-01T00:01:00.000000Z"
PROVIDER_TIME = "2026-08-01T00:02:00.000000Z"
ACCEPTANCE_TIME = "2026-08-02T01:00:00.000000Z"
AUTHORITY_DIGEST = hashlib.sha256(b"v10-f-plan-prepare-fixture").hexdigest()


def fixture_evidence(*, english_head: str = acceptance.EXPECTED_BASELINE) -> dict:
    return {
        "contract_version": "yunos-v9-production-adoption-1",
        "observed_at": ADOPTION_TIME,
        "yunos_git": {
            "branch": "feature/v9-personal-operational-state",
            "head": "3d56896" + ("0" * 33),
            "tracked_change_count": 0,
            "staged_count": 0,
            "untracked_count": 0,
            "evidence_digest": hashlib.sha256(b"yunos-git").hexdigest(),
        },
        "english_git": {
            "branch": "main",
            "head": english_head,
            "tracked_change_count": 13,
            "staged_count": 0,
            "untracked_count": 25,
            "evidence_digest": hashlib.sha256(b"english-git").hexdigest(),
        },
        "main_database": {
            "size": 76_333_056,
            "sha256": hashlib.sha256(b"main-db").hexdigest(),
            "integrity_check": "ok",
            "foreign_key_violations": 0,
            "evidence_digest": hashlib.sha256(b"database").hexdigest(),
        },
        "external_repository_modified": False,
        "full_history_backfill": False,
    }


def file_sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class V10FRealPlanPrepareAcceptanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(
            prefix="ayunos-v10f-plan-prepare-", ignore_cleanup_errors=True
        )
        self.root = Path(self.temp.name).resolve(strict=True)
        self.operational_db = self.root / "operational.db"
        self.runtime_db = self.root / "runtime.db"
        self.output_root = self.root / "acceptance-output"
        self._build_fixture(self.operational_db, self.runtime_db)
        self.target = acceptance._AcceptanceTarget(
            operational_db=self.operational_db,
            runtime_db=self.runtime_db,
            output_root=self.output_root,
            production=False,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _build_fixture(
        self,
        operational: Path,
        runtime: Path,
        *,
        english_head: str = acceptance.EXPECTED_BASELINE,
    ) -> None:
        apply_adoption(
            operational,
            evidence=fixture_evidence(english_head=english_head),
            authority_evidence_digest=AUTHORITY_DIGEST,
            created_at=ADOPTION_TIME,
            production=False,
        )
        apply_cognitive_state_migration(
            operational,
            created_at=MIGRATION_TIME,
        )
        store = OperationalStateStore(operational)
        owner = "writer:test-v10f-provider-seed"
        token = "test-v10f-provider-seed-token-0001"
        store.acquire_writer_lease(
            owner_id=owner,
            token=token,
            acquired_at=PROVIDER_TIME,
            expires_at="2026-08-01T00:03:00.000000Z",
        )
        authority = AuthorityArtifact(
            authority_id="authority:test-v10f-provider-seed",
            issued_by="user:test-v10f",
            issued_at="2026-08-01T00:01:30.000000Z",
            expires_at="2026-08-01T00:03:30.000000Z",
            target_project_id="project:yunos",
            allowed_event_types=("provider_registry_install",),
            read_scope=("provider-registry:yunos-v10",),
            write_scope=("provider-registry:yunos-v10",),
            prohibited_operations=("provider_call",),
            commit_permission=True,
        )
        try:
            CognitiveProviderStore(
                operational, trusted_clock=lambda: PROVIDER_TIME
            ).install_builtin_providers(
                registry=build_suggested_provider_registry(),
                authority=authority,
                lease_owner_id=owner,
                lease_token=token,
            )
        finally:
            store.release_writer_lease(
                owner_id=owner,
                token=token,
                released_at="2026-08-01T00:02:30.000000Z",
            )
        initialized = initialize_runtime_enablement_sidecar(
            runtime,
            execute=True,
            test_mode=True,
        )
        self.assertIn(initialized["status"], {"initialized", "already_initialized"})

    def execute(self) -> dict:
        return acceptance._execute_acceptance(
            target=self.target,
            confirm_user_authorization_sha256=acceptance.USER_AUTHORIZATION_SHA256,
            clock=lambda: ACCEPTANCE_TIME,
        )

    def test_real_plan_prepare_happy_path_and_exact_delta(self) -> None:
        result = self.execute()
        self.assertEqual(
            "YUNOS_V10_F_REAL_PLAN_PREPARE_ACCEPTANCE_GO", result["status"]
        )
        self.assertEqual(
            acceptance.EXPECTED_OPERATIONAL_DELTA,
            result["audit"]["operational_delta"],
        )
        self.assertEqual(0, result["audit"]["writer_lease_final"])
        self.assertEqual(
            "system_verified_not_user_accepted",
            result["audit"]["acceptance_state"],
        )
        self.assertTrue((self.output_root / acceptance.SENTRY_NAME).is_file())
        self.assertTrue((self.output_root / acceptance.RESULT_NAME).is_file())
        self.assertFalse((self.output_root / acceptance.FAILURE_NAME).exists())
        stored = json.loads(
            (self.output_root / acceptance.RESULT_NAME).read_text(encoding="utf-8")
        )
        digest = stored.pop("report_digest")
        self.assertEqual(digest, acceptance.canonical_digest(stored))

    def test_two_independent_objectives_plans_revisions_and_contexts(self) -> None:
        result = self.execute()
        self.assertEqual({"plan", "prepare"}, set(result["objectives"]))
        self.assertNotEqual(
            result["objectives"]["plan"]["objective_id"],
            result["objectives"]["prepare"]["objective_id"],
        )
        for mode in ("plan", "prepare"):
            self.assertEqual(mode, result["objectives"][mode]["agency_level"])
            self.assertEqual("continue", result["objectives"][mode]["cognitive_stance"])
            self.assertEqual(2, result["plans"][mode]["version"])
            self.assertEqual(3, result["plans"][mode]["node_count"])
            self.assertEqual(2, result["plans"][mode]["edge_count"])
            self.assertEqual([], result["plans"][mode]["write_scope"])
            self.assertEqual([], result["plans"][mode]["capabilities"])
            binding = result["context_bindings"][mode]
            self.assertEqual("v9_resume", binding["source_system"])
            self.assertEqual(
                result["project"]["resume_package_digest"], binding["source_digest"]
            )
            self.assertFalse(binding["contains_secret"])

    def test_prepare_preview_authority_acceptance_and_rollback_are_draft_only(self) -> None:
        preview = self.execute()["prepare_preview"]
        self.assertTrue(preview["artifact_only"])
        self.assertTrue(preview["proposed_changes"])
        self.assertTrue(preview["acceptance_conditions"])
        self.assertTrue(preview["rollback_conditions"])
        self.assertFalse(preview["authority_needs"]["currently_granted"])
        self.assertFalse(preview["project_read_performed"])
        self.assertFalse(preview["project_write_performed"])
        self.assertEqual(0, preview["provider_calls"])
        self.assertEqual(0, preview["tool_proposals"])
        self.assertEqual(0, preview["tool_executions"])
        self.assertEqual(
            "system_verified_not_user_accepted", preview["acceptance_state"]
        )

    def test_runs_are_verified_waiting_and_never_self_accept(self) -> None:
        with closing(sqlite3.connect(self.operational_db)) as connection:
            before = {
                table: connection.execute(
                    f'SELECT COUNT(*) FROM "{table}"'
                ).fetchone()[0]
                for table in (
                    "outcome_records",
                    "state_approvals",
                    "authorization_artifacts",
                )
            }
        result = self.execute()
        for mode in ("plan", "prepare"):
            run = result["runs"][mode]
            self.assertEqual("waiting_acceptance", run["status"])
            self.assertIsNone(run["authorization_id"])
            self.assertEqual(0, run["model_calls"])
            self.assertEqual(0, run["tool_proposals"])
            self.assertEqual(0, run["tool_executions"])
        with closing(sqlite3.connect(self.operational_db)) as connection:
            for table, count in before.items():
                self.assertEqual(
                    count,
                    connection.execute(
                        f'SELECT COUNT(*) FROM "{table}"'
                    ).fetchone()[0],
                )

    def test_learning_is_pending_candidate_only(self) -> None:
        result = self.execute()
        self.assertEqual(3, result["learning_candidates"]["count"])
        self.assertEqual("pending", result["learning_candidates"]["status"])
        self.assertFalse(result["learning_candidates"]["long_term_profile_updated"])
        with closing(sqlite3.connect(self.operational_db)) as connection:
            rows = connection.execute(
                "SELECT candidate_type,status,canonical_candidate "
                "FROM state_candidates WHERE candidate_type IN "
                "('fact_record','observation','workflow_candidate') "
                "ORDER BY candidate_type"
            ).fetchall()
        self.assertEqual(3, len(rows))
        self.assertTrue(all(row[1] == "pending" for row in rows))
        self.assertTrue(
            all("auto_adopt_allowed\":true" not in row[2] for row in rows)
        )

    def test_resume_baseline_version_policy_and_project_projection_stay_fixed(self) -> None:
        before = OperationalStateStore(self.operational_db).get_projection(
            acceptance.PROJECT_ID
        )
        result = self.execute()
        after = OperationalStateStore(self.operational_db).get_projection(
            acceptance.PROJECT_ID
        )
        self.assertEqual(before, after)
        self.assertEqual(acceptance.EXPECTED_BASELINE, result["project"]["authoritative_baseline"])
        self.assertEqual(acceptance.EXPECTED_PROJECT_VERSION, result["project"]["current_version"])
        self.assertEqual(acceptance.EXPECTED_EXTERNAL_POLICY, result["project"]["external_repository_policy"])
        resume = OperationalStateContextService(self.operational_db).resume_package(
            acceptance.PROJECT_ID,
            budget_profile="Large",
            as_of="2026-08-02T01:01:00.000000Z",
        )
        self.assertEqual(acceptance.EXPECTED_BASELINE, resume["authoritative_baseline"])
        self.assertEqual(acceptance.EXPECTED_PROJECT_VERSION, resume["current_version"])

    def test_runtime_provider_tool_delegation_outcome_deltas_are_zero(self) -> None:
        runtime_before = file_sha256(self.runtime_db)
        result = self.execute()
        self.assertEqual(runtime_before, file_sha256(self.runtime_db))
        for field in (
            "provider_registry_delta",
            "tool_proposal_delta",
            "tool_execution_delta",
            "delegation_delta",
            "outcome_record_delta",
            "approval_delta",
            "project_completion_update_delta",
            "external_repository_accesses",
        ):
            self.assertEqual(0, result["audit"][field])
        self.assertEqual(0, result["provider"]["calls"])
        self.assertFalse(result["provider"]["hidden_switch"])
        self.assertFalse(result["provider"]["secret_material_read"])

    def test_wrong_baseline_fails_before_sentry_or_writer(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="ayunos-v10f-wrong-baseline-", ignore_cleanup_errors=True
        ) as raw:
            root = Path(raw).resolve(strict=True)
            operational = root / "operational.db"
            runtime = root / "runtime.db"
            output = root / "output"
            self._build_fixture(
                operational,
                runtime,
                english_head="0" * 40,
            )
            target = acceptance._AcceptanceTarget(
                operational_db=operational,
                runtime_db=runtime,
                output_root=output,
                production=False,
            )
            before = acceptance._operational_snapshot(target)
            with self.assertRaisesRegex(
                acceptance.PlanPrepareAcceptanceError,
                "ayun_english_project_boundary_mismatch",
            ):
                acceptance._execute_acceptance(
                    target=target,
                    confirm_user_authorization_sha256=(
                        acceptance.USER_AUTHORIZATION_SHA256
                    ),
                    clock=lambda: ACCEPTANCE_TIME,
                )
            self.assertFalse(output.exists())
            self.assertEqual(before, acceptance._operational_snapshot(target))
            self.assertIsNone(OperationalStateStore(operational).read_status()["lease"])

    def test_exact_authorization_hash_and_one_shot_replay_gate(self) -> None:
        before = acceptance._operational_snapshot(self.target)
        with self.assertRaisesRegex(
            acceptance.PlanPrepareAcceptanceError,
            "user_authorization_confirmation_mismatch",
        ):
            acceptance._execute_acceptance(
                target=self.target,
                confirm_user_authorization_sha256="0" * 64,
                clock=lambda: ACCEPTANCE_TIME,
            )
        self.assertFalse(self.output_root.exists())
        self.assertEqual(before, acceptance._operational_snapshot(self.target))
        self.execute()
        after = acceptance._operational_snapshot(self.target)
        with self.assertRaisesRegex(
            acceptance.PlanPrepareAcceptanceError,
            "plan_prepare_output_root_exists_do_not_rerun",
        ):
            self.execute()
        self.assertEqual(after, acceptance._operational_snapshot(self.target))

    def test_expired_persistence_authority_fails_closed(self) -> None:
        base_text = ACCEPTANCE_TIME
        project, resume = acceptance._project_and_resume_gate(
            self.target, as_of=base_text
        )
        self.assertIsNotNone(project)
        contracts = acceptance._build_contracts(
            resume, datetime.fromisoformat(base_text.replace("Z", "+00:00"))
        )
        expired = replace(
            contracts.authority,
            expires_at="2026-08-02T01:00:01.000000Z",
            authority_digest="",
        )
        contracts = replace(contracts, authority=expired)
        store = OperationalStateStore(self.operational_db)
        owner = "writer:test-expired-authority"
        token = "test-expired-authority-token-0001"
        store.acquire_writer_lease(
            owner_id=owner,
            token=token,
            acquired_at=base_text,
            expires_at="2026-08-02T01:02:00.000000Z",
        )
        try:
            with self.assertRaisesRegex(
                OperationalStateError, "authority_expired_or_not_yet_valid"
            ):
                acceptance._persist_acceptance(
                    target=self.target,
                    contracts=contracts,
                    provider=acceptance._provider_gate(self.target),
                    owner=owner,
                    token=token,
                )
        finally:
            store.release_writer_lease(
                owner_id=owner,
                token=token,
                released_at="2026-08-02T01:00:10.000000Z",
            )
        with closing(sqlite3.connect(self.operational_db)) as connection:
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM objective_contracts"
                ).fetchone()[0],
            )

    def test_hidden_agency_elevation_and_write_overreach_are_rejected(self) -> None:
        _, resume = acceptance._project_and_resume_gate(
            self.target,
            as_of=ACCEPTANCE_TIME,
        )
        contracts = acceptance._build_contracts(
            resume,
            datetime.fromisoformat(ACCEPTANCE_TIME.replace("Z", "+00:00")),
        )
        objective = contracts.objectives["plan"]
        with self.assertRaises(ObjectiveIntentError):
            replace(
                objective,
                agency_selection=replace(
                    objective.agency_selection,
                    maximum_authorized_level="act",
                ),
            )
        node = contracts.plans["prepare"][0].nodes[0]
        overreach = replace(
            node,
            write_scope=("operational://project:ayun-english-core/unauthorized",),
        )
        with self.assertRaises(PlanningError):
            acceptance.build_plan_graph(
                objective=contracts.objectives["prepare"],
                nodes=(overreach, *contracts.plans["prepare"][0].nodes[1:]),
                edges=contracts.plans["prepare"][0].edges,
                change_reason="unauthorized scope injection",
                created_at=ACCEPTANCE_TIME,
            )

    def test_result_write_failure_reconciles_runs_and_releases_writer(self) -> None:
        with patch.object(
            acceptance,
            "_write_result_exclusive",
            side_effect=OSError("injected result write failure"),
        ), self.assertRaises(OSError):
            self.execute()
        self.assertTrue((self.output_root / acceptance.SENTRY_NAME).is_file())
        failure_path = self.output_root / acceptance.FAILURE_NAME
        self.assertTrue(failure_path.is_file())
        failure = json.loads(failure_path.read_text(encoding="utf-8"))
        self.assertEqual("NO_GO_DO_NOT_RERUN", failure["status"])
        self.assertTrue(failure["sanitized"])
        self.assertTrue(failure["recovery"]["writer_lease_zero"])
        self.assertEqual(
            {"failed"}, set(failure["recovery"]["run_states"].values())
        )
        self.assertIsNone(
            OperationalStateStore(self.operational_db).read_status()["lease"]
        )

    def test_mid_persistence_crash_writes_sanitized_recovery_record(self) -> None:
        original = acceptance._persist_context_binding
        calls = 0

        def crash_after_first(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise acceptance.PlanPrepareAcceptanceError(
                    "injected_context_persistence_crash"
                )
            return original(*args, **kwargs)

        with patch.object(
            acceptance, "_persist_context_binding", side_effect=crash_after_first
        ), self.assertRaisesRegex(
            acceptance.PlanPrepareAcceptanceError,
            "injected_context_persistence_crash",
        ):
            self.execute()
        failure = json.loads(
            (self.output_root / acceptance.FAILURE_NAME).read_text(encoding="utf-8")
        )
        self.assertTrue(failure["sanitized"])
        self.assertEqual(0, failure["provider_calls"])
        self.assertEqual(0, failure["tool_executions"])
        self.assertEqual(0, failure["external_repository_accesses"])
        self.assertTrue(failure["recovery"]["writer_lease_zero"])
        self.assertEqual(
            {"not_created"}, set(failure["recovery"]["run_states"].values())
        )

    def test_fixed_production_entry_cli_and_path_security(self) -> None:
        signature = inspect.signature(acceptance.run_production_acceptance)
        self.assertEqual(
            ["confirm_user_authorization_sha256"], list(signature.parameters)
        )
        self.assertNotIn("target", signature.parameters)
        with self.assertRaisesRegex(
            acceptance.PlanPrepareAcceptanceError,
            "shared_core_production_target_forbidden",
        ):
            acceptance._AcceptanceTarget(
                operational_db=self.operational_db,
                runtime_db=self.runtime_db,
                output_root=self.output_root,
                production=True,
            )
        with self.assertRaisesRegex(
            acceptance.PlanPrepareAcceptanceError,
            "external_project_path_forbidden",
        ):
            acceptance._AcceptanceTarget(
                operational_db=self.operational_db,
                runtime_db=self.runtime_db,
                output_root=Path(r"E:\AyunProjects\acceptance-must-not-open"),
                production=False,
            )
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            acceptance.build_parser().parse_args(
                [
                    "execute",
                    "--confirm-user-authorization-sha256",
                    acceptance.USER_AUTHORIZATION_SHA256,
                    "--database",
                    str(self.operational_db),
                ]
            )
        output = io.StringIO()
        with patch.dict(
            os.environ, {acceptance.PRODUCTION_WRITE_ENV: ""}
        ), redirect_stdout(output):
            status = acceptance.main(
                [
                    "execute",
                    "--confirm-user-authorization-sha256",
                    acceptance.USER_AUTHORIZATION_SHA256,
                ]
            )
        self.assertEqual(2, status)
        self.assertIn(
            "production_operational_write_environment_gate_required",
            output.getvalue(),
        )

    def test_imported_shared_core_rejects_production_before_clock_or_io(self) -> None:
        from tools.v10_f_real_plan_prepare_acceptance import (
            _execute_acceptance as imported_execute,
        )

        class PoisonProductionTarget:
            production = True

            def __getattribute__(self, name):
                if name == "production":
                    return True
                raise AssertionError(f"production target property accessed: {name}")

        clock_calls = 0

        def fake_clock() -> str:
            nonlocal clock_calls
            clock_calls += 1
            return ACCEPTANCE_TIME

        with self.assertRaisesRegex(
            acceptance.PlanPrepareAcceptanceError,
            "exact_acceptance_target_required",
        ):
            imported_execute(
                target=PoisonProductionTarget(),
                confirm_user_authorization_sha256=(
                    acceptance.USER_AUTHORIZATION_SHA256
                ),
                clock=fake_clock,
            )
        self.assertEqual(0, clock_calls)

    def test_forged_false_target_and_pin_reject_before_any_dependency(self) -> None:
        forged = SimpleNamespace(
            production=False,
            operational_db=acceptance.PRODUCTION_OPERATIONAL_DB,
            runtime_db=acceptance.PRODUCTION_RUNTIME_DB,
            output_root=acceptance.PRODUCTION_OUTPUT_ROOT,
        )
        clock = Mock(side_effect=AssertionError("clock reached"))
        path_probe = Mock(side_effect=AssertionError("path reached"))
        store = Mock(side_effect=AssertionError("store reached"))
        with patch.object(
            acceptance, "_reject_reparse_chain", path_probe
        ), patch.object(acceptance, "OperationalStateStore", store):
            with self.assertRaisesRegex(
                acceptance.PlanPrepareAcceptanceError,
                "exact_acceptance_target_required",
            ):
                acceptance._execute_acceptance(
                    target=forged,
                    confirm_user_authorization_sha256=(
                        acceptance.USER_AUTHORIZATION_SHA256
                    ),
                    clock=clock,
                )
            with self.assertRaisesRegex(
                acceptance.PlanPrepareAcceptanceError,
                "exact_acceptance_target_required",
            ):
                acceptance._pin_new_output_root(forged)
            with self.assertRaisesRegex(
                acceptance.PlanPrepareAcceptanceError,
                "exact_acceptance_target_required",
            ):
                acceptance._persist_context_binding(
                    target=forged,
                    binding=None,
                    objective=None,
                    anchor=None,
                    authority=None,
                    owner="owner",
                    token="token",
                    created_at=ACCEPTANCE_TIME,
                )
        forged_pin = SimpleNamespace(
            path=acceptance.PRODUCTION_OUTPUT_ROOT,
            device=0,
            inode=0,
            anchor_path=acceptance.PRODUCTION_OUTPUT_ROOT / ".output-root-anchor",
            anchor_device=0,
            anchor_inode=0,
            anchor_descriptor=0,
        )
        with patch.object(
            acceptance.os, "open", side_effect=AssertionError("open reached")
        ):
            with self.assertRaisesRegex(
                acceptance.PlanPrepareAcceptanceError,
                "exact_pinned_output_required",
            ):
                acceptance._write_exclusive(forged_pin, "result.json", {})
        exact_pin = object.__new__(acceptance._PinnedOutput)
        for name, value in (
            ("path", acceptance.PRODUCTION_OUTPUT_ROOT),
            ("device", 0),
            ("inode", 0),
            (
                "anchor_path",
                acceptance.PRODUCTION_OUTPUT_ROOT / ".output-root-anchor",
            ),
            ("anchor_device", 0),
            ("anchor_inode", 0),
            ("anchor_descriptor", 0),
        ):
            object.__setattr__(exact_pin, name, value)
        fstat = Mock(side_effect=AssertionError("fstat reached"))
        with patch.object(acceptance.os, "fstat", fstat):
            with self.assertRaisesRegex(
                acceptance.PlanPrepareAcceptanceError,
                "shared_core_production_path_forbidden",
            ):
                acceptance._write_exclusive(exact_pin, "result.json", {})
        clock.assert_not_called()
        path_probe.assert_not_called()
        store.assert_not_called()
        fstat.assert_not_called()

    def test_object_new_and_windows_namespace_formal_targets_fail_pre_io(self) -> None:
        cases = (
            (
                acceptance.PRODUCTION_OPERATIONAL_DB,
                acceptance.PRODUCTION_RUNTIME_DB,
                acceptance.PRODUCTION_OUTPUT_ROOT,
                "shared_core_production_path_forbidden",
            ),
            (
                Path(r"\\?\E:\AyunOS\data\operational\ayunos_operational_state.db"),
                Path(r"\\?\E:\AyunOS\data\runtime\ayunos_runtime.db"),
                Path(
                    r"\\?\E:\AyunOS\output\v10-f-production-adoption\real-plan-prepare-acceptance"
                ),
                "shared_target_windows_namespace_forbidden",
            ),
        )
        for operational, runtime, output, reason in cases:
            forged = object.__new__(acceptance._AcceptanceTarget)
            object.__setattr__(forged, "operational_db", operational)
            object.__setattr__(forged, "runtime_db", runtime)
            object.__setattr__(forged, "output_root", output)
            object.__setattr__(forged, "production", False)
            clock = Mock(side_effect=AssertionError("clock reached"))
            path_probe = Mock(side_effect=AssertionError("path reached"))
            store = Mock(side_effect=AssertionError("store reached"))
            with self.subTest(reason=reason), patch.object(
                acceptance, "_reject_reparse_chain", path_probe
            ), patch.object(acceptance, "OperationalStateStore", store):
                with self.assertRaisesRegex(
                    acceptance.PlanPrepareAcceptanceError,
                    reason,
                ):
                    acceptance._execute_acceptance(
                        target=forged,
                        confirm_user_authorization_sha256=(
                            acceptance.USER_AUTHORIZATION_SHA256
                        ),
                        clock=clock,
                    )
            clock.assert_not_called()
            path_probe.assert_not_called()
            store.assert_not_called()

    def test_windows_device_unc_ads_drive_prefix_and_traversal_are_rejected(self) -> None:
        temp_root = Path(tempfile.gettempdir()).absolute()
        temp_drive = os.path.splitdrive(str(temp_root))[0]
        other_drive = "Z:" if temp_drive.casefold() != "z:" else "C:"
        cases = (
            (
                Path(
                    r"\\?\E:\AyunOS\output\v10-f-production-adoption\real-plan-prepare-acceptance"
                ),
                "shared_target_windows_namespace_forbidden",
            ),
            (
                Path(
                    r"e:\aYuNoS\OuTpUt\V10-F-PrOdUcTiOn-AdOpTiOn\ReAl-PlAn-PrEpArE-AcCePtAnCe"
                ),
                "shared_core_production_path_forbidden",
            ),
            (Path(r"\\.\E:\device-output"), "shared_target_windows_namespace_forbidden"),
            (Path(r"\\server\share\output"), "shared_target_windows_namespace_forbidden"),
            (self.root / "output:stream", "shared_target_ads_forbidden"),
            (Path(f"{other_drive}\\output"), "shared_target_drive_forbidden"),
            (
                Path(str(temp_root) + "-prefix-confusion") / "output",
                "shared_target_outside_system_temp",
            ),
            (
                self.root / ".." / self.root.name / "output",
                "shared_target_traversal_forbidden",
            ),
        )
        for output, reason in cases:
            with self.subTest(output=str(output), reason=reason), self.assertRaisesRegex(
                acceptance.PlanPrepareAcceptanceError,
                reason,
            ):
                acceptance._AcceptanceTarget(
                    operational_db=self.operational_db,
                    runtime_db=self.runtime_db,
                    output_root=output,
                    production=False,
                )

    def test_temp_hardlink_alias_and_mocked_reparse_are_rejected(self) -> None:
        nested = self.root / "nested-output-parent"
        nested.mkdir()
        original = acceptance.is_reparse_point

        def fake_reparse(path):
            return Path(path) == nested or original(path)

        with patch.object(
            acceptance, "is_reparse_point", side_effect=fake_reparse
        ), self.assertRaisesRegex(
            acceptance.PlanPrepareAcceptanceError,
            "shared_target_reparse_forbidden",
        ):
            acceptance._AcceptanceTarget(
                operational_db=self.operational_db,
                runtime_db=self.runtime_db,
                output_root=nested / "output",
                production=False,
            )

        hardlink = self.root / "operational-hardlink.db"
        os.link(self.operational_db, hardlink)
        with self.assertRaisesRegex(
            acceptance.PlanPrepareAcceptanceError,
            "shared_target_database_alias_forbidden",
        ):
            acceptance._AcceptanceTarget(
                operational_db=hardlink,
                runtime_db=self.runtime_db,
                output_root=self.output_root,
                production=False,
            )

    def test_every_target_core_rebuilds_an_exact_validated_target_first(self) -> None:
        guarded = {
            acceptance._pin_new_output_root,
            acceptance._operational_snapshot,
            acceptance._runtime_snapshot,
            acceptance._project_and_resume_gate,
            acceptance._provider_gate,
            acceptance._persist_context_binding,
            acceptance._create_nonexecuting_run,
            acceptance._transition_run,
            acceptance._record_verifications_and_candidates,
            acceptance._persist_acceptance,
            acceptance._audit_persisted_result,
            acceptance._failure_reconcile,
            acceptance._execute_acceptance,
        }
        for callable_value in guarded:
            tree = ast.parse(textwrap.dedent(inspect.getsource(callable_value)))
            definition = tree.body[0]
            first_statement = definition.body[0]
            self.assertIsInstance(
                first_statement,
                ast.Assign,
                msg=callable_value.__name__,
            )
            self.assertEqual(
                "target = _rebuild_shared_target(target)",
                ast.unparse(first_statement),
                msg=callable_value.__name__,
            )
        for callable_value in (
            acceptance._assert_output_identity,
            acceptance._write_exclusive,
            acceptance._write_result_exclusive,
            acceptance._release_output_anchor,
        ):
            tree = ast.parse(textwrap.dedent(inspect.getsource(callable_value)))
            first_statement = tree.body[0].body[0]
            self.assertEqual(
                "output = _rebuild_pinned_output(output)",
                ast.unparse(first_statement),
                msg=callable_value.__name__,
            )
        post_init = ast.parse(
            textwrap.dedent(inspect.getsource(acceptance._AcceptanceTarget.__post_init__))
        ).body[0].body[0]
        self.assertIn("_validate_shared_target_fields(self)", ast.unparse(post_init))
        for callable_value, exact_test in (
            (
                acceptance._validate_shared_target_fields,
                "type(target) is not _AcceptanceTarget",
            ),
            (
                acceptance._rebuild_shared_target,
                "type(target) is not _AcceptanceTarget",
            ),
            (
                acceptance._rebuild_pinned_output,
                "type(output) is not _PinnedOutput",
            ),
        ):
            first_statement = ast.parse(
                textwrap.dedent(inspect.getsource(callable_value))
            ).body[0].body[0]
            self.assertIsInstance(first_statement, ast.If)
            self.assertEqual(exact_test, ast.unparse(first_statement.test))

    def test_fixed_executor_reflection_has_no_injection_or_singleton_surface(self) -> None:
        public = acceptance.run_production_acceptance
        unbound = acceptance._FixedProductionExecutor.run
        self.assertIsNone(public.__closure__)
        self.assertIsNone(public.__defaults__)
        self.assertIsNone(public.__kwdefaults__)
        self.assertIsNone(unbound.__closure__)
        self.assertIsNone(unbound.__defaults__)
        self.assertIsNone(unbound.__kwdefaults__)
        self.assertEqual(
            ["confirm_user_authorization_sha256"],
            list(inspect.signature(public).parameters),
        )
        self.assertEqual(
            ["self", "confirm_user_authorization_sha256"],
            list(inspect.signature(unbound).parameters),
        )
        self.assertEqual((), fields(acceptance._FixedProductionExecutor))
        self.assertNotIn("_FIXED_PRODUCTION_EXECUTOR", acceptance.__dict__)
        self.assertFalse(
            any(
                isinstance(value, acceptance._FixedProductionExecutor)
                for value in acceptance.__dict__.values()
            )
        )
        for name, function in inspect.getmembers(acceptance, inspect.isfunction):
            if function.__module__ != acceptance.__name__:
                continue
            self.assertIsNone(function.__closure__, msg=name)
            defaults = tuple(function.__defaults__ or ()) + tuple(
                (function.__kwdefaults__ or {}).values()
            )
            self.assertFalse(
                any(
                    isinstance(value, acceptance._AcceptanceTarget)
                    or value
                    in {
                        acceptance.PRODUCTION_OPERATIONAL_DB,
                        acceptance.PRODUCTION_RUNTIME_DB,
                        acceptance.PRODUCTION_OUTPUT_ROOT,
                    }
                    or callable(value)
                    for value in defaults
                    if value is not None
                ),
                msg=name,
            )
        referenced_globals = inspect.getclosurevars(unbound).globals
        self.assertEqual(
            {
                "PlanPrepareAcceptanceError",
                "_FixedProductionExecutor",
                "_build_contracts",
                "_candidate_anchor",
                "_fail",
                "_objective_event",
                "_plan_event",
                "_prepare_preview",
                "_run_identity",
            },
            set(referenced_globals),
        )
        for forbidden in (
            "PRODUCTION_OPERATIONAL_DB",
            "PRODUCTION_RUNTIME_DB",
            "PRODUCTION_OUTPUT_ROOT",
            "_execute_acceptance",
            "_project_and_resume_gate",
            "_provider_gate",
            "_operational_snapshot",
            "_runtime_snapshot",
            "_pin_new_output_root",
            "_persist_acceptance",
            "_audit_persisted_result",
            "_failure_reconcile",
            "_write_exclusive",
            "_write_result_exclusive",
            "_utc_now",
        ):
            self.assertNotIn(forbidden, referenced_globals)
        production_source = inspect.getsource(unbound)
        for forbidden_call in (
            "_execute_acceptance(",
            "_project_and_resume_gate(",
            "_provider_gate(",
            "_operational_snapshot(",
            "_runtime_snapshot(",
            "_pin_new_output_root(",
            "_persist_acceptance(",
            "_audit_persisted_result(",
            "_failure_reconcile(",
            "_write_exclusive(",
            "_write_result_exclusive(",
        ):
            self.assertNotIn(forbidden_call, production_source)

    def test_fixed_executor_rejects_missing_provider_before_mapping_access(self) -> None:
        production_source = inspect.getsource(
            acceptance._FixedProductionExecutor.run
        )
        missing_gate = production_source.index(
            "if not isinstance(provider, dict):"
        )
        first_mapping_access = production_source.index(
            'provider.get("provider_id")'
        )
        output_creation = production_source.index(
            "output_root.mkdir(exist_ok=False)"
        )
        first_write_connection = production_source.index(
            "production_store._connect(write=True)"
        )
        missing_failure = production_source.index(
            '_fail("persisted_provider_profile_missing_or_invalid")',
            missing_gate,
        )
        self.assertLess(missing_gate, first_mapping_access)
        self.assertLess(missing_gate, output_creation)
        self.assertLess(missing_gate, first_write_connection)
        self.assertLess(missing_failure, first_mapping_access)
        self.assertLess(missing_failure, output_creation)
        self.assertLess(missing_failure, first_write_connection)

    def test_fake_target_clock_source_and_unbound_calls_cannot_reach_production(self) -> None:
        production_output = acceptance.PRODUCTION_OUTPUT_ROOT
        before = (
            production_output.exists(),
            production_output.stat().st_mtime_ns
            if production_output.exists()
            else None,
        )
        with self.assertRaises(TypeError):
            acceptance.run_production_acceptance(
                confirm_user_authorization_sha256=(
                    acceptance.USER_AUTHORIZATION_SHA256
                ),
                target=self.target,
            )
        with self.assertRaises(TypeError):
            acceptance._FixedProductionExecutor.run(
                acceptance._FixedProductionExecutor(),
                confirm_user_authorization_sha256=(
                    acceptance.USER_AUTHORIZATION_SHA256
                ),
                clock=lambda: ACCEPTANCE_TIME,
            )
        with self.assertRaises(TypeError):
            acceptance._FixedProductionExecutor.run(
                acceptance._FixedProductionExecutor(),
                confirm_user_authorization_sha256=(
                    acceptance.USER_AUTHORIZATION_SHA256
                ),
                source_builder=object(),
            )
        with self.assertRaisesRegex(
            acceptance.PlanPrepareAcceptanceError,
            "exact_production_executor_required",
        ):
            acceptance._FixedProductionExecutor.run(
                object(),
                confirm_user_authorization_sha256=(
                    acceptance.USER_AUTHORIZATION_SHA256
                ),
            )
        for production in (False, True):
            with self.assertRaisesRegex(
                acceptance.PlanPrepareAcceptanceError,
                (
                    "shared_core_production_path_forbidden"
                    if not production
                    else "shared_core_production_target_forbidden"
                ),
            ):
                acceptance._AcceptanceTarget(
                    operational_db=acceptance.PRODUCTION_OPERATIONAL_DB,
                    runtime_db=acceptance.PRODUCTION_RUNTIME_DB,
                    output_root=acceptance.PRODUCTION_OUTPUT_ROOT,
                    production=production,
                )
        with patch.multiple(
            acceptance,
            PRODUCTION_OPERATIONAL_DB=self.operational_db,
            PRODUCTION_RUNTIME_DB=self.runtime_db,
            PRODUCTION_OUTPUT_ROOT=self.output_root,
        ), self.assertRaisesRegex(
            acceptance.PlanPrepareAcceptanceError,
            "shared_core_production_path_forbidden",
        ):
            acceptance._AcceptanceTarget(
                operational_db=Path(
                    r"E:\AyunOS\data\operational\ayunos_operational_state.db"
                ),
                runtime_db=Path(r"E:\AyunOS\data\runtime\ayunos_runtime.db"),
                output_root=Path(
                    r"E:\AyunOS\output\v10-f-production-adoption\real-plan-prepare-acceptance"
                ),
                production=False,
            )
        after = (
            production_output.exists(),
            production_output.stat().st_mtime_ns
            if production_output.exists()
            else None,
        )
        self.assertEqual(before, after)

    def test_output_anchor_remains_open_through_result_publication(self) -> None:
        original = acceptance._write_result_exclusive
        observed: dict[str, object] = {}

        def inspected_publication(output, payload):
            descriptor_stat = os.fstat(output.anchor_descriptor)
            anchor_stat = output.anchor_path.stat()
            observed["open"] = True
            observed["identity"] = (
                descriptor_stat.st_dev,
                descriptor_stat.st_ino,
            ) == (anchor_stat.st_dev, anchor_stat.st_ino)
            original(output, payload)
            os.fstat(output.anchor_descriptor)

        with patch.object(
            acceptance,
            "_write_result_exclusive",
            side_effect=inspected_publication,
        ):
            self.execute()
        self.assertEqual({"open": True, "identity": True}, observed)
        self.assertTrue((self.output_root / ".output-root-anchor").is_file())

    def test_source_has_no_provider_transport_tool_or_external_repo_access(self) -> None:
        source = inspect.getsource(acceptance)
        for forbidden in (
            "subprocess",
            "urlopen",
            "requests.",
            "DeepSeekV4Adapter",
            "ProductionRuntimeExecutionService",
            "CognitiveToolGateway",
            "DelegationService",
            "create_tool_proposal",
            "E:\\AyunProjects\\",
        ):
            self.assertNotIn(forbidden, source)
        self.assertNotIn("provider_adapter", source)
        self.assertNotIn("transport=", source)


if __name__ == "__main__":
    unittest.main()
