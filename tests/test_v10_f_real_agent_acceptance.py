from __future__ import annotations

import ast
from contextlib import closing, redirect_stdout
from datetime import datetime, timezone
import gc
import hashlib
import inspect
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from memory.cognitive_agent_runtime_source import ProductionAgentRuntimeTurnSource
from memory.cognitive_orchestration import ContextItem, ContextSource, ModelBudgetState
from memory.cognitive_runtime_bridge import create_mock_cognitive_runtime_bridge
from memory.cognitive_state_migration import apply_cognitive_state_migration
from memory.deepseek_v4_adapter import DeepSeekV4Response
from memory.operational_state_delegation import DelegationService
from memory.operational_state_models import canonical_digest
from memory.operational_state_store import (
    OperationalStateStore,
    initialize_operational_database,
)
from memory.runtime_protocol_contracts import (
    PreparedRuntimeRequest,
    bounded_tool_exact_response_from_prepared_request,
)
from memory.runtime_enablement import initialize_runtime_enablement_sidecar
from tools import v10_f_real_agent_acceptance as acceptance


class V10FRealAgentAcceptanceBoundaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="ayunos-v10f-agent-")
        self.root = Path(self.temp.name).resolve(strict=True)
        self.operational_db = self.root / "operational.db"
        self.runtime_db = self.root / "runtime.db"
        self.output_root = self.root / "acceptance-output"
        initialize_operational_database(
            self.operational_db,
            created_at="2026-08-01T00:00:00.000000Z",
        )
        apply_cognitive_state_migration(
            self.operational_db,
            created_at="2026-08-01T00:01:00.000000Z",
        )
        self.provider_setup = acceptance.build_deepseek_provider_setup(
            {"status": "mock_healthy", "network_calls": 0}
        )
        self.confirmation = acceptance.ControllerConfirmation(
            execute=True,
            allow_production_operational_write=True,
            allow_official_readiness_gets=True,
            confirm_one_shot=True,
            confirm_two_deepseek_tool_proposals=True,
            confirm_disposable_sandbox=True,
            user_authorization_sha256=acceptance.USER_CONSENT_TEXT_SHA256,
            combined_boundary_sha256=(
                acceptance.USER_APPROVED_COMBINED_BOUNDARY_SHA256
            ),
            production=False,
        )
        self.sandbox = self.output_root / acceptance.SANDBOX_NAME
        self.contracts = acceptance._build_contracts(
            sandbox_root=self.sandbox,
            provider_setup=self.provider_setup,
            base=datetime.now(timezone.utc),
        )

    def tearDown(self) -> None:
        # CPython's sqlite statement cache can form an unreachable cycle; collect
        # after each case so Windows can deterministically remove the closed DB.
        gc.collect()
        self.temp.cleanup()

    @staticmethod
    def _file_sha256(path: Path) -> str | None:
        if not path.is_file():
            return None
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def _sources(self) -> tuple[ContextSource, ...]:
        now = acceptance._utc_now()
        definitions = (
            (
                "v7_memory",
                "memory-package:test-v10-f",
                "untrusted_memory_evidence",
                "unverifiable",
                "immutable_evidence",
                "not-applicable",
                "immutable_source_evidence",
                None,
                {"summary": "Bounded historical evidence for the test only."},
            ),
            (
                "v9_now",
                "now-package:test-v10-f",
                "untrusted_operational_state",
                "fresh",
                "live",
                "no-store",
                "live_operational_state",
                now,
                {"current_phase": "v10-F test fixture", "state": "active"},
            ),
            (
                "v9_resume",
                "resume-package:test-v10-f",
                "untrusted_operational_state",
                "fresh",
                "live",
                "no-store",
                "live_operational_state",
                now,
                {"current_version": "v10-F", "next_approved_step": "test only"},
            ),
        )
        sources = []
        for (
            system,
            reference,
            trust,
            freshness,
            state,
            cache_control,
            cache_classification,
            as_of,
            content,
        ) in definitions:
            item = ContextItem(
                item_id=f"ctxitem:test-v10-f-{system}",
                content=content,
                priority=0,
                trust_class=trust,
            )
            sources.append(
                ContextSource(
                    source_system=system,
                    source_reference=reference,
                    source_digest=canonical_digest(content),
                    purpose=f"Bind {system} as data only for a disposable test.",
                    items=(item,),
                    maximum_items=1,
                    maximum_bytes=2_048,
                    freshness=freshness,
                    source_state=state,
                    cache_control=cache_control,
                    cache_classification=cache_classification,
                    cached_at=None,
                    cache_stale=False,
                    as_of=as_of,
                    required=True,
                    priority=10,
                )
            )
        return tuple(sources)

    def test_provider_health_clock_matches_runtime_bridge_seconds_precision(self) -> None:
        checked_at = acceptance._runtime_bridge_compatible_now()
        self.assertRegex(checked_at, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        setup = acceptance.build_deepseek_provider_setup(
            {"status": "mock_healthy", "network_calls": 0},
            checked_at=checked_at,
        )
        attestation = setup.selection.health_attestation
        self.assertIsNotNone(attestation)
        normalized = acceptance.normalize_timestamp(checked_at)
        self.assertEqual(normalized, attestation.checked_at)

    def test_production_context_packages_share_the_evaluation_timestamp(self) -> None:
        evaluated_at = "2026-08-02T01:23:45.000000Z"
        memory_package = object()
        now_package = {"package": "now"}
        resume_package = {"package": "resume"}
        service = Mock()
        service.now_package.return_value = now_package
        service.resume_package.return_value = resume_package
        v7_source = object()
        now_source = object()
        resume_source = object()

        with (
            patch.object(
                acceptance,
                "build_production_memory_package",
                return_value=memory_package,
            ),
            patch.object(
                acceptance,
                "OperationalStateContextService",
                return_value=service,
            ),
            patch.object(
                acceptance,
                "context_source_from_v7_memory_package",
                return_value=v7_source,
            ),
            patch.object(
                acceptance,
                "context_source_from_v9_package",
                side_effect=(now_source, resume_source),
            ) as from_v9,
        ):
            sources = acceptance.production_context_sources(
                evaluated_at=evaluated_at
            )

        self.assertEqual((v7_source, now_source, resume_source), sources)
        service.now_package.assert_called_once_with(
            budget_profile="Small",
            as_of=evaluated_at,
        )
        service.resume_package.assert_called_once_with(
            acceptance.PROJECT_ID,
            budget_profile="Small",
            as_of=evaluated_at,
        )
        self.assertEqual(2, from_v9.call_count)
        for call in from_v9.call_args_list:
            self.assertEqual(evaluated_at, call.kwargs["evaluated_at"])

    def _typed_success_response(
        self,
        prepared_request: PreparedRuntimeRequest,
        *,
        response_id: str,
    ) -> DeepSeekV4Response:
        self.assertIsInstance(prepared_request, PreparedRuntimeRequest)
        content = bounded_tool_exact_response_from_prepared_request(
            prepared_request
        )
        parsed = json.loads(content)
        self.assertTrue(parsed["payload"]["proposal_only"])
        self.assertFalse(parsed["payload"]["authorization_claimed"])
        self.assertFalse(parsed["payload"]["execution_claimed"])
        return DeepSeekV4Response(
            provider_response_id=response_id,
            model=self.contracts.provider_selection.model,
            content=content,
            usage={
                "prompt_tokens": 96,
                "completion_tokens": 24,
                "total_tokens": 120,
            },
            finish_reason="stop",
        )

    def test_contracts_are_exactly_two_actions_and_deny_release_operations(self) -> None:
        nodes = self.contracts.plan.nodes
        self.assertEqual(2, len(nodes))
        self.assertTrue(all(node.node_kind == "action" for node in nodes))
        by_id = {}
        for node in nodes:
            ids = {item.capability_id for item in node.capability_requirements}
            if "process.template.run" in ids:
                by_id["process.template.run"] = node
            if "project.file.patch" in ids:
                by_id["project.file.patch"] = node
        self.assertEqual(
            {"project.file.patch", "process.template.run"}, set(by_id)
        )
        self.assertEqual(
            (by_id["project.file.patch"].node_id,),
            by_id["process.template.run"].dependencies,
        )
        self.assertEqual(
            ["project.file.patch"],
            [
                item.capability_id
                for item in by_id["project.file.patch"].capability_requirements
            ],
        )
        self.assertEqual(2, len(self.contracts.rollback_plan.nodes))
        rollback_nodes = {
            node.capability_requirements[0].capability_id: node
            for node in self.contracts.rollback_plan.nodes
        }
        self.assertEqual(
            {"project.file.patch", "project.change.rollback"},
            set(rollback_nodes),
        )
        self.assertEqual(
            (rollback_nodes["project.file.patch"].node_id,),
            rollback_nodes["project.change.rollback"].dependencies,
        )
        authorization = self.contracts.authorization
        self.assertEqual(2, authorization.max_steps)
        self.assertEqual(2, authorization.max_model_calls)
        self.assertFalse(authorization.commit_permission)
        self.assertFalse(authorization.merge_permission)
        self.assertFalse(authorization.tag_permission)
        self.assertFalse(authorization.push_permission)
        self.assertFalse(authorization.network_permission)
        self.assertEqual("none", authorization.database_permission)
        self.assertFalse(authorization.next_version_creation_permission)
        self.assertFalse(authorization.persistent_background_agent_permission)
        self.assertEqual(("git.diff.check.v1",), authorization.allowed_commands)
        self.assertFalse(self.contracts.upstream_authority.commit_permission)
        self.assertNotIn("*", self.contracts.upstream_authority.read_scope)
        self.assertNotIn("*", self.contracts.upstream_authority.write_scope)
        self.assertEqual(
            (str(self.sandbox.resolve(strict=False)),),
            self.contracts.upstream_authority.read_scope,
        )
        self.assertEqual(
            (
                str(
                    (
                        self.sandbox / acceptance.AUTHORIZED_RELATIVE_ROOT
                    ).resolve(strict=False)
                ),
            ),
            self.contracts.upstream_authority.write_scope,
        )

    def test_context_factory_binds_one_typed_blueprint_per_plan_node(self) -> None:
        factory = acceptance.build_bounded_context_factory(
            objective=self.contracts.objective,
            sources=self._sources(),
            capability_registry=self.contracts.capability_registry,
        )
        run = SimpleNamespace(
            objective_id=self.contracts.objective.objective_id,
            plan_id=self.contracts.plan.plan_id,
            authorization_id=self.contracts.authorization.authorization_id,
        )
        blueprints = []
        for node in self.contracts.plan.nodes:
            context = factory(
                run,
                self.contracts.plan,
                node,
                self.contracts.authorization,
            )
            payload = dict(context.to_dict()["model_context"])
            self.assertNotIn("bounded_plan_node", payload)
            blueprint = payload["bounded_tool_proposal_blueprint"]
            expected_requirement = next(
                item
                for item in node.capability_requirements
                if item.capability_id == blueprint["capability_id"]
            )
            self.assertEqual(
                expected_requirement.to_dict(),
                {
                    key: blueprint[key]
                    for key in (
                        "capability_id",
                        "capability_version",
                        "capability_digest",
                    )
                },
            )
            self.assertEqual(node.risk, blueprint["risk_class"])
            blueprints.append(blueprint)
        process_blueprint, create_blueprint = sorted(
            blueprints, key=lambda item: item["capability_id"]
        )
        self.assertEqual("process.template.run", process_blueprint["capability_id"])
        self.assertEqual(
            "git.diff.check", process_blueprint["typed_arguments"]["template_id"]
        )
        self.assertEqual("project.file.patch", create_blueprint["capability_id"])
        self.assertEqual(
            acceptance.BASELINE_CONTENT_SHA256,
            create_blueprint["typed_arguments"]["expected_before_sha256"],
        )
        self.assertEqual(
            acceptance.PATCHED_CONTENT_SHA256,
            create_blueprint["typed_arguments"]["expected_after_sha256"],
        )

    def test_temp_persistence_and_push_overreach_do_not_touch_production(self) -> None:
        production_before = self._file_sha256(acceptance.PRODUCTION_OPERATIONAL_DB)
        operational = OperationalStateStore(self.operational_db)
        owner = "owner:test-v10-f-agent"
        token = "test-v10-f-agent-writer-token"
        operational.acquire_writer_lease(
            owner_id=owner,
            token=token,
            acquired_at=acceptance._utc_now(),
            expires_at=acceptance._stamp(datetime.now(timezone.utc), 900),
        )
        try:
            execution_store, planning_store, run = acceptance._persist_contracts(
                target=acceptance.AcceptanceTarget(
                    operational_db=self.operational_db,
                    runtime_db=self.runtime_db,
                    output_root=self.output_root,
                    production=False,
                ),
                contracts=self.contracts,
                owner=owner,
                token=token,
            )
            self.assertEqual("queued", run.status)
            self.assertEqual(
                self.contracts.plan.plan_digest,
                planning_store.get_plan(
                    self.contracts.plan.plan_id,
                    self.contracts.plan.version,
                )["plan_digest"],
            )
            self.assertEqual(
                self.contracts.authorization.authority_digest,
                execution_store.get_authorization(
                    self.contracts.authorization.authorization_id
                ).authority_digest,
            )
            service = DelegationService(
                operational,
                source_anchor=self.contracts.anchor,
            )
            blocked = service.enforce_action(
                delegation_id=self.contracts.overreach_delegation.delegation_id,
                proposed_action={"action": "push", "remote": "origin"},
                authority=self.contracts.authority,
                agent_id=acceptance.AGENT_ID,
                at=acceptance._utc_now(),
                lease_owner_id=owner,
                lease_token=token,
            )
            self.assertEqual("agent_overreach_blocked", blocked["reason_code"])
            self.assertFalse(blocked["operation_executed"])
            self.assertIn("push_not_authorized", blocked["violations"])
            self.assertIn("contract_prohibited_action", blocked["violations"])
        finally:
            operational.release_writer_lease(
                owner_id=owner,
                token=token,
                released_at=acceptance._utc_now(),
            )
        self.assertIsNone(operational.read_status()["lease"])
        self.assertEqual(
            production_before,
            self._file_sha256(acceptance.PRODUCTION_OPERATIONAL_DB),
        )

    def test_two_prepared_turns_share_the_authorized_cost_ceiling(self) -> None:
        operational = OperationalStateStore(self.operational_db)
        owner = "owner:test-v10-f-budget"
        token = "test-v10-f-budget-writer-token"
        operational.acquire_writer_lease(
            owner_id=owner,
            token=token,
            acquired_at=acceptance._utc_now(),
            expires_at=acceptance._stamp(datetime.now(timezone.utc), 900),
        )
        try:
            execution_store, _planning_store, run = acceptance._persist_contracts(
                target=acceptance.AcceptanceTarget(
                    operational_db=self.operational_db,
                    runtime_db=self.runtime_db,
                    output_root=self.output_root,
                    production=False,
                ),
                contracts=self.contracts,
                owner=owner,
                token=token,
            )
            bridge = create_mock_cognitive_runtime_bridge(self.runtime_db)
            source = acceptance.mock_source_builder(bridge)(
                objective=self.contracts.objective,
                provider_registry=self.contracts.provider_registry,
                provider_selection=self.contracts.provider_selection,
                context_factory=acceptance.build_bounded_context_factory(
                    objective=self.contracts.objective,
                    sources=self._sources(),
                    capability_registry=self.contracts.capability_registry,
                ),
                consent_factory=acceptance.build_external_consent_factory(
                    confirmation=self.confirmation,
                    objective=self.contracts.objective,
                    controller_binding=(
                        acceptance.build_controller_confirmation_binding(
                            confirmation=self.confirmation,
                            contracts=self.contracts,
                            run_id=run.run_id,
                        )
                    ),
                ),
            )
            self.assertIsInstance(source, ProductionAgentRuntimeTurnSource)
            state = ModelBudgetState()
            turns = []
            with patch(
                "memory.production_runtime_execution._VisibleMockAdapter.send",
                side_effect=AssertionError("transport must not be used"),
            ) as send:
                for node in self.contracts.plan.nodes:
                    turn = source.prepare_turn(
                        run=run,
                        plan=self.contracts.plan,
                        node=node,
                        authorization=self.contracts.authorization,
                        budget_state=state,
                    )
                    turns.append(turn)
                    state = turn.budget_state_after_reservation
                send.assert_not_called()
            self.assertEqual(2, state.calls_reserved)
            self.assertEqual(0, len(state.settled_reservation_ids))
            self.assertEqual(
                self.contracts.authorization.max_cost_microunits,
                sum(turn.reservation.cost_microunits_ceiling for turn in turns),
            )
            self.assertTrue(
                all(
                    turn.request.expected_output_kind == "tool_proposal"
                    and turn.reservation.grants_authority is False
                    and turn.reservation.execution_allowed is False
                    for turn in turns
                )
            )
            self.assertEqual("queued", execution_store.get_run(run.run_id).status)
        finally:
            operational.release_writer_lease(
                owner_id=owner,
                token=token,
                released_at=acceptance._utc_now(),
            )
        self.assertIsNone(operational.read_status()["lease"])

    def test_mock_e2e_is_typed_exactly_once_and_waits_for_acceptance(self) -> None:
        target = acceptance.AcceptanceTarget(
            operational_db=self.operational_db,
            runtime_db=self.runtime_db,
            output_root=self.output_root,
            production=False,
        )
        production_before = {
            "operational": self._file_sha256(acceptance.PRODUCTION_OPERATIONAL_DB),
            "runtime": self._file_sha256(acceptance.PRODUCTION_RUNTIME_DB),
        }
        initialized_runtime = initialize_runtime_enablement_sidecar(
            self.runtime_db,
            execute=True,
            test_mode=True,
        )
        self.assertEqual("initialized", initialized_runtime["status"])
        bridge = create_mock_cognitive_runtime_bridge(self.runtime_db)
        prepared_requests: list[PreparedRuntimeRequest] = []
        blocked_dispatch_swaps: list[int | None] = []
        blocked_result_swaps: list[int | None] = []
        original_result_writer = acceptance._write_result_exclusive

        def typed_mock_send(_adapter, prepared_request):
            self.assertIsInstance(prepared_request, PreparedRuntimeRequest)
            if os.name == "nt" and not blocked_dispatch_swaps:
                try:
                    self.output_root.rename(self.root / "dispatch-swap")
                except OSError as error:
                    blocked_dispatch_swaps.append(error.winerror)
                else:
                    self.fail("dispatch began without the retained sentry guard")
            exact_response = bounded_tool_exact_response_from_prepared_request(
                prepared_request
            )
            parsed = json.loads(exact_response)
            self.assertTrue(parsed["payload"]["proposal_only"])
            self.assertFalse(parsed["payload"]["authorization_claimed"])
            self.assertFalse(parsed["payload"]["execution_claimed"])
            prepared_requests.append(prepared_request)
            return DeepSeekV4Response(
                provider_response_id=f"mock-v10-f-{len(prepared_requests)}",
                model=self.contracts.provider_selection.model,
                content=exact_response,
                usage={
                    "prompt_tokens": 96,
                    "completion_tokens": 24,
                    "total_tokens": 120,
                },
                finish_reason="stop",
            )

        def result_writer_with_swap_probe(path: Path, payload) -> None:
            if os.name == "nt":
                try:
                    self.output_root.rename(self.root / "result-publication-swap")
                except OSError as error:
                    blocked_result_swaps.append(error.winerror)
                else:
                    self.fail("output root rename escaped its publication handle")
            original_result_writer(path, payload)

        with patch(
            "memory.production_runtime_execution._VisibleMockAdapter.send",
            new=typed_mock_send,
        ), patch.object(
            acceptance,
            "_write_result_exclusive",
            new=result_writer_with_swap_probe,
        ):
            result = acceptance.execute_acceptance(
                target=target,
                provider_setup=self.provider_setup,
                sources=self._sources(),
                source_builder=acceptance.mock_source_builder(bridge),
                confirmation=self.confirmation,
            )

        self.assertEqual("GO", result["status"])
        self.assertEqual(
            acceptance._EXPECTED_OPERATIONAL_DELTA,
            result["operational"]["delta"],
        )
        self.assertEqual("waiting_acceptance", result["agent"]["status"])
        self.assertEqual(2, result["agent"]["model_sends"])
        self.assertEqual(0, result["agent"]["replay_model_sends"])
        self.assertEqual(2, len(prepared_requests))
        if os.name == "nt":
            self.assertEqual(1, len(blocked_dispatch_swaps))
            self.assertIn(blocked_dispatch_swaps[0], {5, 32})
            self.assertEqual(1, len(blocked_result_swaps))
            self.assertIn(blocked_result_swaps[0], {5, 32})
        self.assertEqual(2, len({item.artifact_identity for item in prepared_requests}))
        self.assertEqual(
            ["diff", "--check", "--", acceptance.CREATED_RELATIVE_PATH],
            result["sandbox"]["git_diff"]["diff_check_resolved_argv"][-4:],
        )
        self.assertFalse(result["outcome_boundary"]["user_accepted"])
        self.assertTrue(result["outcome_boundary"]["system_verified"])
        self.assertEqual(
            ["fact_record", "observation", "workflow_candidate"],
            result["learning"]["candidate_types"],
        )
        self.assertTrue(result["learning"]["all_pending"])
        self.assertFalse(result["learning"]["long_term_profile_updated"])
        self.assertFalse(result["overreach"]["operation_executed"])
        self.assertIsNone(result["writer"]["lease_after"])
        self.assertEqual(
            result["runtime"]["expected_delta"],
            result["runtime"]["delta"],
        )
        self.assertEqual(
            acceptance.PATCHED_CONTENT_SHA256,
            self._file_sha256(
                self.output_root
                / acceptance.SANDBOX_NAME
                / acceptance.CREATED_RELATIVE_PATH
            ),
        )
        self.assertEqual(
            {
                "artifact": acceptance.SENTRY_NAME,
                "held_before_dispatch": True,
                "delete_sharing": False,
            },
            result["sandbox"]["publication_guard"],
        )
        sentry = json.loads(
            (self.output_root / acceptance.SENTRY_NAME).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(acceptance.SENTRY_NAME, sentry["retained_guard_artifact"])
        self.assertTrue(sentry["retained_guard_required_before_dispatch"])
        self.assertTrue(result["sandbox"]["rollback_rehearsal"]["backup_reference_verified"])
        self.assertEqual(
            "cancelled",
            result["sandbox"]["rollback_rehearsal"]["run_status"],
        )
        self.assertEqual(0, result["sandbox"]["rollback_rehearsal"]["model_calls_used"])
        self.assertEqual(
            2,
            result["sandbox"]["rollback_rehearsal"]["gateway_execution_count"],
        )
        self.assertEqual(
            0,
            result["sandbox"]["rollback_rehearsal"]["direct_broker_side_effects"],
        )
        self.assertEqual(
            "tool_path_outside_authorized_write_root",
            result["overreach"]["reason_code"],
        )
        self.assertEqual(0, result["overreach"]["tool_execution_delta"])
        self.assertTrue(result["trusted_external_send"]["trusted_user_event_verified"])
        self.assertEqual(2, len(result["trusted_external_send"]["trusted_user_event_ids"]))
        self.assertEqual(2, len(set(result["trusted_external_send"]["consent_ids"])))
        consent_artifact = json.loads(
            Path(result["trusted_external_send"]["consent_artifact"]).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            [1, 2],
            [item["turn_sequence"] for item in consent_artifact["records"]],
        )
        self.assertEqual(
            acceptance.USER_APPROVED_COMBINED_BOUNDARY_SHA256,
            consent_artifact["controller_binding"][
                "user_approved_combined_boundary_sha256"
            ],
        )
        self.assertEqual(
            acceptance.EXTERNAL_SEND_DISCLOSURE_DIGEST,
            consent_artifact["controller_binding"][
                "agent_external_send_disclosure_digest"
            ],
        )
        self.assertEqual(
            2,
            len(
                {
                    item["external_send_consent"][
                        "runtime_disclosure_digest"
                    ]
                    for item in consent_artifact["records"]
                }
            ),
        )
        for item in consent_artifact["records"]:
            event = item["trusted_user_event"]
            consent = item["external_send_consent"]
            self.assertEqual(
                "我知情并批准上述 DeepSeek 外发边界。",
                event["user_utterance"],
            )
            self.assertEqual(
                acceptance.USER_CONSENT_TEXT_SHA256,
                event["user_utterance_sha256"],
            )
            self.assertGreater(
                datetime.fromisoformat(
                    event["observed_at"].replace("Z", "+00:00")
                ),
                datetime.fromisoformat(
                    consent["runtime_disclosure_prepared_at"].replace(
                        "Z", "+00:00"
                    )
                ),
            )
        self.assertEqual(2, len(result["runtime"]["model_run_ids"]))
        self.assertTrue(result["runtime"]["mock_transport"])
        self.assertEqual("ok", result["operational"]["integrity_check"])
        self.assertEqual(0, result["operational"]["foreign_key_violations"])
        self.assertEqual(
            production_before,
            {
                "operational": self._file_sha256(
                    acceptance.PRODUCTION_OPERATIONAL_DB
                ),
                "runtime": self._file_sha256(acceptance.PRODUCTION_RUNTIME_DB),
            },
        )

    def test_second_model_failure_preserves_both_runtime_receipts(self) -> None:
        target = acceptance.AcceptanceTarget(
            operational_db=self.operational_db,
            runtime_db=self.runtime_db,
            output_root=self.output_root,
            production=False,
        )
        initialize_runtime_enablement_sidecar(
            self.runtime_db,
            execute=True,
            test_mode=True,
        )
        bridge = create_mock_cognitive_runtime_bridge(self.runtime_db)
        calls = 0

        def fail_second(_adapter, prepared_request):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("injected_second_model_failure")
            return self._typed_success_response(
                prepared_request,
                response_id="mock-v10-f-first-succeeded",
            )

        with patch(
            "memory.production_runtime_execution._VisibleMockAdapter.send",
            new=fail_second,
        ), self.assertRaises(acceptance.V10FRealAgentAcceptanceError):
            acceptance.execute_acceptance(
                target=target,
                provider_setup=self.provider_setup,
                sources=self._sources(),
                source_builder=acceptance.mock_source_builder(bridge),
                confirmation=self.confirmation,
            )
        self.assertEqual(2, calls)
        failure = json.loads(
            (self.output_root / acceptance.FAILURE_NAME).read_text(
                encoding="utf-8"
            )
        )
        bindings = failure["recovery"]["runtime_bindings"]
        self.assertEqual(2, len(bindings))
        self.assertEqual(
            2,
            len(
                {
                    item["runtime_execution_reference"]
                    for item in bindings
                }
            ),
        )
        self.assertTrue(
            all(
                isinstance(item["runtime_receipt_identity"], str)
                and isinstance(item["runtime_receipt_digest"], str)
                for item in bindings
            )
        )
        runtime = acceptance.MockRuntimeExecutionService(self.runtime_db)
        receipts = [
            runtime.get_run(item["runtime_execution_reference"])
            for item in bindings
        ]
        self.assertEqual(["succeeded", "failed"], [item["status"] for item in receipts])
        self.assertEqual(
            [item["runtime_receipt_identity"] for item in bindings],
            [item["receipt_identity"] for item in receipts],
        )
        self.assertTrue(failure["recovery"]["audit_event_ids"])
        self.assertIsNone(
            OperationalStateStore(self.operational_db).read_status()["lease"]
        )

    def test_malformed_structured_output_is_not_retried_and_is_recoverable(self) -> None:
        target = acceptance.AcceptanceTarget(
            operational_db=self.operational_db,
            runtime_db=self.runtime_db,
            output_root=self.output_root,
            production=False,
        )
        initialize_runtime_enablement_sidecar(
            self.runtime_db,
            execute=True,
            test_mode=True,
        )
        bridge = create_mock_cognitive_runtime_bridge(self.runtime_db)
        calls = 0

        def malformed_send(_adapter, _prepared_request):
            nonlocal calls
            calls += 1
            return DeepSeekV4Response(
                provider_response_id="mock-v10-f-malformed-structured-output",
                model=self.contracts.provider_selection.model,
                content="```json\n{not valid structured output}\n```",
                usage={
                    "prompt_tokens": 96,
                    "completion_tokens": 12,
                    "total_tokens": 108,
                },
                finish_reason="stop",
            )

        with patch(
            "memory.production_runtime_execution._VisibleMockAdapter.send",
            new=malformed_send,
        ), self.assertRaises(Exception):
            acceptance.execute_acceptance(
                target=target,
                provider_setup=self.provider_setup,
                sources=self._sources(),
                source_builder=acceptance.mock_source_builder(bridge),
                confirmation=self.confirmation,
            )
        self.assertEqual(1, calls)
        failure = json.loads(
            (self.output_root / acceptance.FAILURE_NAME).read_text(encoding="utf-8")
        )
        self.assertTrue(failure["do_not_rerun"])
        self.assertEqual(1, len(failure["recovery"]["runtime_bindings"]))
        binding = failure["recovery"]["runtime_bindings"][0]
        receipt = acceptance.MockRuntimeExecutionService(self.runtime_db).get_run(
            binding["runtime_execution_reference"]
        )
        self.assertEqual("succeeded", receipt["status"])
        self.assertEqual(receipt["receipt_identity"], binding["runtime_receipt_identity"])
        self.assertIsNone(
            OperationalStateStore(self.operational_db).read_status()["lease"]
        )

    def test_result_write_failure_recovers_completed_execution_state(self) -> None:
        target = acceptance.AcceptanceTarget(
            operational_db=self.operational_db,
            runtime_db=self.runtime_db,
            output_root=self.output_root,
            production=False,
        )
        initialize_runtime_enablement_sidecar(
            self.runtime_db,
            execute=True,
            test_mode=True,
        )
        bridge = create_mock_cognitive_runtime_bridge(self.runtime_db)
        calls = 0

        def typed_send(_adapter, prepared_request):
            nonlocal calls
            calls += 1
            return self._typed_success_response(
                prepared_request,
                response_id=f"mock-v10-f-result-write-{calls}",
            )

        with patch(
            "memory.production_runtime_execution._VisibleMockAdapter.send",
            new=typed_send,
        ), patch.object(
            acceptance,
            "_write_result_exclusive",
            side_effect=acceptance.V10FRealAgentAcceptanceError(
                "injected_result_write_failure"
            ),
        ), self.assertRaisesRegex(
            acceptance.V10FRealAgentAcceptanceError,
            "injected_result_write_failure",
        ):
            acceptance.execute_acceptance(
                target=target,
                provider_setup=self.provider_setup,
                sources=self._sources(),
                source_builder=acceptance.mock_source_builder(bridge),
                confirmation=self.confirmation,
            )
        self.assertEqual(2, calls)
        failure = json.loads(
            (self.output_root / acceptance.FAILURE_NAME).read_text(
                encoding="utf-8"
            )
        )
        self.assertTrue(failure["sanitized"])
        self.assertEqual(
            "injected_result_write_failure", failure["reason_code"]
        )
        self.assertEqual(2, len(failure["recovery"]["runtime_bindings"]))
        self.assertTrue(failure["recovery"]["audit_event_ids"])
        self.assertTrue(
            all(
                state in {"failed", "cancelled", "recovery_required"}
                for state in failure["recovery"]["run_states"].values()
            )
        )
        self.assertTrue(
            all(
                state in {"completed", "failed", "cancelled", "rejected"}
                for state in failure["recovery"][
                    "delegation_states"
                ].values()
            )
        )
        with closing(sqlite3.connect(self.operational_db)) as connection:
            active_runs = connection.execute(
                "SELECT COUNT(*) FROM cognitive_runs WHERE status IN "
                "('queued','reasoning','planning','waiting_authority',"
                "'executing','waiting_external','verifying',"
                "'waiting_acceptance')"
            ).fetchone()[0]
        self.assertEqual(0, active_runs)
        self.assertIsNone(
            OperationalStateStore(self.operational_db).read_status()["lease"]
        )

    def test_sentry_and_finally_release_survive_pre_transport_failure(self) -> None:
        target = acceptance.AcceptanceTarget(
            operational_db=self.operational_db,
            runtime_db=self.runtime_db,
            output_root=self.output_root,
            production=False,
        )
        production_before = self._file_sha256(acceptance.PRODUCTION_OPERATIONAL_DB)

        def fake_sandbox_init(
            path: Path,
            *,
            output_target: acceptance.AcceptanceTarget | None = None,
            directory_pins: acceptance._DirectoryHandlePins | None = None,
        ) -> None:
            self.assertIs(target, output_target)
            self.assertIsNotNone(directory_pins)
            path.mkdir(parents=True, exist_ok=False)
            (path / ".git").mkdir()

        source_calls = 0

        def stop_before_transport(**_kwargs):
            nonlocal source_calls
            source_calls += 1
            raise acceptance.V10FRealAgentAcceptanceError(
                "injected_stop_before_transport"
            )

        with patch.object(
            acceptance,
            "_initialize_disposable_git_sandbox",
            side_effect=fake_sandbox_init,
        ), self.assertRaisesRegex(
            acceptance.V10FRealAgentAcceptanceError,
            "injected_stop_before_transport",
        ):
            acceptance.execute_acceptance(
                target=target,
                provider_setup=self.provider_setup,
                sources=self._sources(),
                source_builder=stop_before_transport,
                confirmation=self.confirmation,
            )
        self.assertEqual(1, source_calls)
        self.assertTrue((self.output_root / acceptance.SENTRY_NAME).is_file())
        self.assertTrue((self.output_root / acceptance.FAILURE_NAME).is_file())
        self.assertIsNone(OperationalStateStore(self.operational_db).read_status()["lease"])
        failure_record = json.loads(
            (self.output_root / acceptance.FAILURE_NAME).read_text(
                encoding="utf-8"
            )
        )
        self.assertTrue(failure_record["sanitized"])
        self.assertEqual(
            "injected_stop_before_transport", failure_record["reason_code"]
        )
        self.assertTrue(failure_record["writer_lease_released"])
        self.assertTrue(failure_record["recovery"]["audit_event_ids"])
        self.assertTrue(
            all(
                state in {"failed", "recovery_required"}
                for state in failure_record["recovery"]["run_states"].values()
            )
        )
        self.assertTrue(
            all(
                state in {"failed", "cancelled"}
                for state in failure_record["recovery"][
                    "delegation_states"
                ].values()
            )
        )
        serialized_failure = json.dumps(failure_record, ensure_ascii=False)
        self.assertNotIn("DEEPSEEK_API_KEY", serialized_failure)
        self.assertNotIn("wire_body_base64", serialized_failure)

        with self.assertRaisesRegex(
            acceptance.V10FRealAgentAcceptanceError,
            "real_agent_output_root_already_exists",
        ):
            acceptance.execute_acceptance(
                target=target,
                provider_setup=self.provider_setup,
                sources=self._sources(),
                source_builder=stop_before_transport,
                confirmation=self.confirmation,
            )
        self.assertEqual(1, source_calls)
        self.assertEqual(
            production_before,
            self._file_sha256(acceptance.PRODUCTION_OPERATIONAL_DB),
        )

    def test_cli_is_dry_without_every_confirmation(self) -> None:
        output = io.StringIO()
        with patch.object(
            acceptance,
            "run_production_acceptance",
            side_effect=AssertionError("production path must not run"),
        ) as run, redirect_stdout(output):
            status = acceptance.main(["--execute"])
        self.assertEqual(0, status)
        run.assert_not_called()
        rendered = output.getvalue()
        self.assertIn('"external_model_calls": 0', rendered)
        self.assertIn('"production_database_writes": 0', rendered)

    def test_cli_rejects_the_pre_disclosure_authorization_hash(self) -> None:
        old_authorization_sha256 = hashlib.sha256(
            "授权，继续".encode("utf-8")
        ).hexdigest()
        self.assertNotEqual(
            acceptance.USER_CONSENT_TEXT_SHA256,
            old_authorization_sha256,
        )
        output = io.StringIO()
        arguments = [
            "--execute",
            "--allow-production-operational-write",
            "--allow-official-readiness-gets",
            "--confirm-one-shot",
            "--confirm-two-deepseek-tool-proposals",
            "--confirm-disposable-sandbox",
            "--confirm-user-authorization-sha256",
            old_authorization_sha256,
            "--confirm-combined-boundary-sha256",
            acceptance.USER_APPROVED_COMBINED_BOUNDARY_SHA256,
        ]
        with patch.object(
            acceptance,
            "run_production_acceptance",
            side_effect=AssertionError("production path must not run"),
        ) as run, redirect_stdout(output):
            status = acceptance.main(arguments)
        self.assertEqual(0, status)
        run.assert_not_called()
        self.assertIn(
            '"required_flags": ["--confirm-user-authorization-sha256"]',
            output.getvalue(),
        )

    def test_cli_requires_the_exact_combined_boundary_digest(self) -> None:
        production_before = {
            "operational": self._file_sha256(
                acceptance.PRODUCTION_OPERATIONAL_DB
            ),
            "runtime": self._file_sha256(acceptance.PRODUCTION_RUNTIME_DB),
        }
        base_arguments = [
            "--execute",
            "--allow-production-operational-write",
            "--allow-official-readiness-gets",
            "--confirm-one-shot",
            "--confirm-two-deepseek-tool-proposals",
            "--confirm-disposable-sandbox",
            "--confirm-user-authorization-sha256",
            acceptance.USER_CONSENT_TEXT_SHA256,
        ]
        for label, supplied in (
            ("missing", None),
            ("wrong", "0" * 64),
        ):
            with self.subTest(label=label):
                arguments = list(base_arguments)
                if supplied is not None:
                    arguments.extend(
                        ["--confirm-combined-boundary-sha256", supplied]
                    )
                output = io.StringIO()
                with patch.object(
                    acceptance,
                    "run_production_acceptance",
                    side_effect=AssertionError("production path must not run"),
                ) as run, redirect_stdout(output):
                    status = acceptance.main(arguments)
                self.assertEqual(0, status)
                run.assert_not_called()
                rendered = json.loads(output.getvalue())
                self.assertEqual("dry_run", rendered["status"])
                self.assertEqual(0, rendered["external_model_calls"])
                self.assertEqual(0, rendered["production_database_writes"])
                self.assertEqual(
                    ["--confirm-combined-boundary-sha256"],
                    rendered["required_flags"],
                )
        with self.assertRaisesRegex(
            acceptance.V10FRealAgentAcceptanceError,
            "controller_combined_boundary_sha256_mismatch",
        ):
            acceptance.ControllerConfirmation(
                execute=True,
                allow_production_operational_write=True,
                allow_official_readiness_gets=True,
                confirm_one_shot=True,
                confirm_two_deepseek_tool_proposals=True,
                confirm_disposable_sandbox=True,
                user_authorization_sha256=acceptance.USER_CONSENT_TEXT_SHA256,
                combined_boundary_sha256="0" * 64,
                production=False,
            )
        self.assertEqual(
            production_before,
            {
                "operational": self._file_sha256(
                    acceptance.PRODUCTION_OPERATIONAL_DB
                ),
                "runtime": self._file_sha256(
                    acceptance.PRODUCTION_RUNTIME_DB
                ),
            },
        )

    def test_full_cli_flags_still_require_the_process_environment_gate(self) -> None:
        production_before = self._file_sha256(acceptance.PRODUCTION_OPERATIONAL_DB)
        output = io.StringIO()
        arguments = [
            "--execute",
            "--allow-production-operational-write",
            "--allow-official-readiness-gets",
            "--confirm-one-shot",
            "--confirm-two-deepseek-tool-proposals",
            "--confirm-disposable-sandbox",
            "--confirm-user-authorization-sha256",
            acceptance.USER_CONSENT_TEXT_SHA256,
            "--confirm-combined-boundary-sha256",
            acceptance.USER_APPROVED_COMBINED_BOUNDARY_SHA256,
        ]
        with patch.dict(
            os.environ,
            {acceptance.PRODUCTION_WRITE_ENV: ""},
        ), redirect_stdout(output):
            status = acceptance.main(arguments)
        self.assertEqual(2, status)
        self.assertIn(
            "production_operational_write_environment_gate_required",
            output.getvalue(),
        )
        self.assertEqual(
            production_before,
            self._file_sha256(acceptance.PRODUCTION_OPERATIONAL_DB),
        )

    def test_target_paths_are_fail_closed(self) -> None:
        with self.assertRaisesRegex(
            acceptance.V10FRealAgentAcceptanceError,
            "production_acceptance_path_mismatch",
        ):
            acceptance.AcceptanceTarget(
                operational_db=self.operational_db,
                runtime_db=self.runtime_db,
                output_root=self.output_root,
                production=True,
            )

    def test_production_entry_rejects_all_dependency_injection(self) -> None:
        production_before = {
            "operational": self._file_sha256(
                acceptance.PRODUCTION_OPERATIONAL_DB
            ),
            "runtime": self._file_sha256(acceptance.PRODUCTION_RUNTIME_DB),
        }
        target = acceptance.AcceptanceTarget(
            operational_db=acceptance.PRODUCTION_OPERATIONAL_DB,
            runtime_db=acceptance.PRODUCTION_RUNTIME_DB,
            output_root=acceptance.PRODUCTION_OUTPUT_ROOT,
            production=True,
        )
        production_confirmation = acceptance.ControllerConfirmation(
            execute=True,
            allow_production_operational_write=True,
            allow_official_readiness_gets=True,
            confirm_one_shot=True,
            confirm_two_deepseek_tool_proposals=True,
            confirm_disposable_sandbox=True,
            user_authorization_sha256=acceptance.USER_CONSENT_TEXT_SHA256,
            combined_boundary_sha256=(
                acceptance.USER_APPROVED_COMBINED_BOUNDARY_SHA256
            ),
            production=True,
        )
        with self.assertRaisesRegex(
            acceptance.V10FRealAgentAcceptanceError,
            "production_acceptance_dependency_injection_forbidden",
        ):
            acceptance.execute_acceptance(
                target=target,
                provider_setup=self.provider_setup,
                sources=self._sources(),
                source_builder=lambda **_kwargs: None,
                confirmation=production_confirmation,
            )
        forged_context = object.__new__(acceptance._AcceptanceExecutionContext)
        for name, value in {
            "target": target,
            "provider_setup": self.provider_setup,
            "sources": self._sources(),
            "source_builder": lambda **_kwargs: None,
            "confirmation": production_confirmation,
        }.items():
            object.__setattr__(forged_context, name, value)
        with patch.object(
            acceptance,
            "verify_cognitive_state_schema",
        ) as schema_probe, patch.object(
            acceptance,
            "open_production_deepseek_readiness",
        ) as readiness_open, patch.object(
            acceptance,
            "run_production_deepseek_readiness",
        ) as readiness_run:
            with self.assertRaisesRegex(
                acceptance.V10FRealAgentAcceptanceError,
                "production_acceptance_dependency_injection_forbidden",
            ):
                acceptance._new_test_execution_context(
                    target=target,
                    provider_setup=self.provider_setup,
                    sources=self._sources(),
                    source_builder=lambda **_kwargs: None,
                    confirmation=production_confirmation,
                )
            with self.assertRaisesRegex(
                acceptance.V10FRealAgentAcceptanceError,
                "production_acceptance_dependency_injection_forbidden",
            ):
                acceptance._execute_acceptance_context(forged_context)
            schema_probe.assert_not_called()
            readiness_open.assert_not_called()
            readiness_run.assert_not_called()
        self.assertEqual(
            production_before,
            {
                "operational": self._file_sha256(
                    acceptance.PRODUCTION_OPERATIONAL_DB
                ),
                "runtime": self._file_sha256(
                    acceptance.PRODUCTION_RUNTIME_DB
                ),
            },
        )
        self.assertFalse(hasattr(acceptance, "_PRODUCTION_EXECUTION_SEAL"))
        self.assertFalse(hasattr(acceptance, "_execute_acceptance_implementation"))
        self.assertFalse(hasattr(acceptance, "_build_acceptance_entrypoints"))
        self.assertFalse(hasattr(acceptance, "_FIXED_PRODUCTION_SOURCE_BUILDER"))
        self.assertFalse(hasattr(acceptance, "_FixedProductionSourceBuilder"))
        self.assertEqual(
            {"confirmation"},
            set(inspect.signature(acceptance.run_production_acceptance).parameters),
        )
        self.assertIsNone(acceptance.run_production_acceptance.__closure__)
        self.assertEqual(
            {},
            inspect.getclosurevars(
                acceptance.run_production_acceptance
            ).nonlocals,
        )
        self.assertEqual(
            {"self", "confirmation"},
            set(
                inspect.signature(
                    acceptance._FixedProductionAcceptanceExecutor.run
                ).parameters
            ),
        )
        self.assertIsNone(
            acceptance._FixedProductionAcceptanceExecutor.run.__closure__
        )
        self.assertEqual(
            {"run"},
            {
                name
                for name, value in vars(
                    acceptance._FixedProductionAcceptanceExecutor
                ).items()
                if inspect.isfunction(value)
            },
        )
        production_run_source = inspect.getsource(
            acceptance._FixedProductionAcceptanceExecutor.run
        )
        self.assertIn("def source_builder", production_run_source)
        self.assertNotIn("_execute_acceptance_context", production_run_source)
        module_tree = ast.parse(
            Path(acceptance.__file__).read_text(encoding="utf-8")
        )
        shared_node = next(
            node
            for node in module_tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_execute_acceptance_context"
        )
        executor_node = next(
            node
            for node in module_tree.body
            if isinstance(node, ast.ClassDef)
            and node.name == "_FixedProductionAcceptanceExecutor"
        )
        production_run_node = next(
            node
            for node in executor_node.body
            if isinstance(node, ast.FunctionDef) and node.name == "run"
        )
        self.assertEqual(
            "target.production is not False",
            ast.unparse(shared_node.body[2].test),
        )
        self.assertFalse(
            any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_execute_acceptance_context"
                for node in ast.walk(production_run_node)
            )
        )
        self.assertEqual(
            ["source_builder"],
            [
                node.name
                for node in production_run_node.body
                if isinstance(node, ast.FunctionDef)
            ],
        )

        def schema_index(statements):
            return next(
                index
                for index, node in enumerate(statements)
                if isinstance(node, ast.Assign)
                and any(
                    isinstance(target_node, ast.Name)
                    and target_node.id == "schema"
                    for target_node in node.targets
                )
            )

        shared_schema_index = schema_index(shared_node.body)
        production_schema_index = schema_index(production_run_node.body)
        self.assertEqual(
            [
                ast.dump(node, include_attributes=False)
                for node in shared_node.body[shared_schema_index:]
            ],
            [
                ast.dump(node, include_attributes=False)
                for node in production_run_node.body[production_schema_index:]
            ],
        )
        self.assertEqual((), tuple(inspect.signature(
            acceptance._AcceptanceExecutionContext
        ).parameters))

        raw_dependencies = {
            "target",
            "provider_setup",
            "sources",
            "source_builder",
        }
        raw_module_callables: list[str] = []
        raw_class_callables: list[str] = []
        raw_closure_callables: list[str] = []
        for name, value in vars(acceptance).items():
            if inspect.isfunction(value) and value.__module__ == acceptance.__name__:
                if raw_dependencies <= set(inspect.signature(value).parameters):
                    raw_module_callables.append(name)
                for cell in value.__closure__ or ():
                    try:
                        captured = cell.cell_contents
                    except ValueError:
                        continue
                    if callable(captured):
                        try:
                            parameters = set(inspect.signature(captured).parameters)
                        except (TypeError, ValueError):
                            continue
                        if raw_dependencies <= parameters:
                            raw_closure_callables.append(name)
            if inspect.isclass(value) and value.__module__ == acceptance.__name__:
                for member_name, member in vars(value).items():
                    if isinstance(member, (staticmethod, classmethod)):
                        member = member.__func__
                    if not inspect.isfunction(member):
                        continue
                    if raw_dependencies <= set(inspect.signature(member).parameters):
                        raw_class_callables.append(f"{name}.{member_name}")
        self.assertEqual(
            ["_new_test_execution_context", "execute_acceptance"],
            sorted(raw_module_callables),
        )
        self.assertEqual([], raw_class_callables)
        self.assertEqual([], raw_closure_callables)
        with patch.dict(
            os.environ,
            {acceptance.PRODUCTION_WRITE_ENV: "1"},
        ), patch.object(
            acceptance,
            "open_production_deepseek_readiness",
        ) as readiness_open, patch.object(
            acceptance,
            "verify_cognitive_state_schema",
        ) as schema_probe, self.assertRaisesRegex(
            acceptance.V10FRealAgentAcceptanceError,
            "production_controller_confirmation_required",
        ):
            acceptance._FixedProductionAcceptanceExecutor().run(
                confirmation=self.confirmation,
            )
        readiness_open.assert_not_called()
        schema_probe.assert_not_called()
        with patch.dict(
            os.environ,
            {acceptance.PRODUCTION_WRITE_ENV: "1"},
        ), patch.object(
            acceptance,
            "open_production_deepseek_readiness",
        ) as readiness_open, patch.object(
            acceptance,
            "verify_cognitive_state_schema",
        ) as schema_probe, self.assertRaisesRegex(
            acceptance.V10FRealAgentAcceptanceError,
            "exact_production_acceptance_executor_required",
        ):
            acceptance._FixedProductionAcceptanceExecutor.run(
                object(),
                confirmation=production_confirmation,
            )
        readiness_open.assert_not_called()
        schema_probe.assert_not_called()

    def test_production_entry_rechecks_forged_combined_boundary_confirmation(
        self,
    ) -> None:
        production_before = {
            "operational": self._file_sha256(
                acceptance.PRODUCTION_OPERATIONAL_DB
            ),
            "runtime": self._file_sha256(acceptance.PRODUCTION_RUNTIME_DB),
        }
        output_existed_before = acceptance.PRODUCTION_OUTPUT_ROOT.exists()

        def valid_confirmation():
            return acceptance.ControllerConfirmation(
                execute=True,
                allow_production_operational_write=True,
                allow_official_readiness_gets=True,
                confirm_one_shot=True,
                confirm_two_deepseek_tool_proposals=True,
                confirm_disposable_sandbox=True,
                user_authorization_sha256=acceptance.USER_CONSENT_TEXT_SHA256,
                combined_boundary_sha256=(
                    acceptance.USER_APPROVED_COMBINED_BOUNDARY_SHA256
                ),
                production=True,
            )

        mutated = valid_confirmation()
        object.__setattr__(mutated, "combined_boundary_sha256", "0" * 64)
        source = valid_confirmation()
        reconstructed = object.__new__(acceptance.ControllerConfirmation)
        for name, value in source.to_dict().items():
            object.__setattr__(reconstructed, name, value)
        object.__setattr__(
            reconstructed,
            "combined_boundary_sha256",
            "f" * 64,
        )

        for label, forged in (
            ("object-setattr", mutated),
            ("object-new", reconstructed),
        ):
            with self.subTest(label=label), patch.dict(
                os.environ,
                {acceptance.PRODUCTION_WRITE_ENV: "1"},
            ), patch.object(
                acceptance,
                "AcceptanceTarget",
            ) as target_factory, patch.object(
                acceptance,
                "open_production_deepseek_readiness",
            ) as readiness_open, patch.object(
                acceptance,
                "run_production_deepseek_readiness",
            ) as readiness_run, patch.object(
                acceptance,
                "create_production_agent_runtime_source",
            ) as provider_source, patch.object(
                acceptance,
                "verify_cognitive_state_schema",
            ) as schema_probe, patch.object(
                acceptance,
                "_write_pinned_output_exclusive",
            ) as output_writer, self.assertRaisesRegex(
                acceptance.V10FRealAgentAcceptanceError,
                "production_controller_confirmation_required",
            ):
                acceptance.run_production_acceptance(
                    confirmation=forged,
                )
            target_factory.assert_not_called()
            readiness_open.assert_not_called()
            readiness_run.assert_not_called()
            provider_source.assert_not_called()
            schema_probe.assert_not_called()
            output_writer.assert_not_called()

        self.assertEqual(
            output_existed_before,
            acceptance.PRODUCTION_OUTPUT_ROOT.exists(),
        )
        self.assertEqual(
            production_before,
            {
                "operational": self._file_sha256(
                    acceptance.PRODUCTION_OPERATIONAL_DB
                ),
                "runtime": self._file_sha256(
                    acceptance.PRODUCTION_RUNTIME_DB
                ),
            },
        )

    def test_output_root_parent_identity_and_reparse_are_fail_closed(self) -> None:
        target = acceptance.AcceptanceTarget(
            operational_db=self.operational_db,
            runtime_db=self.runtime_db,
            output_root=self.output_root,
            production=False,
        )
        changed = tuple(
            value + 1 if index == 1 else value
            for index, value in enumerate(target.output_anchor_identity)
        )
        with patch.object(
            acceptance,
            "_filesystem_identity",
            return_value=changed,
        ), self.assertRaisesRegex(
            acceptance.V10FRealAgentAcceptanceError,
            "acceptance_output_parent_identity_changed",
        ):
            acceptance._create_pinned_output_root(target)
        self.assertFalse(self.output_root.exists())

        acceptance._create_pinned_output_root(target)
        self.assertIsNotNone(target.output_root_identity)
        original_identity = acceptance._filesystem_identity
        changed_root_identity = tuple(
            value + 1 if index == 1 else value
            for index, value in enumerate(target.output_root_identity or ())
        )

        def identity_after_root_swap(path: Path):
            if Path(path) == target.output_root:
                return changed_root_identity
            return original_identity(Path(path))

        drift_name = "post-create-identity-drift.json"
        with patch.object(
            acceptance,
            "_filesystem_identity",
            side_effect=identity_after_root_swap,
        ), self.assertRaisesRegex(
            acceptance.V10FRealAgentAcceptanceError,
            "acceptance_output_root_identity_changed",
        ):
            acceptance._write_pinned_output_exclusive(
                target=target,
                name=drift_name,
                payload={"must_not_write": True},
            )
        self.assertFalse((target.output_root / drift_name).exists())

        reparse_name = "post-create-reparse-drift.json"
        with patch.object(
            acceptance,
            "is_reparse_point",
            side_effect=lambda path: Path(path) == target.output_root,
        ), self.assertRaisesRegex(
            acceptance.V10FRealAgentAcceptanceError,
            "acceptance_output_reparse_denied",
        ):
            acceptance._write_pinned_output_exclusive(
                target=target,
                name=reparse_name,
                payload={"must_not_write": True},
            )
        self.assertFalse((target.output_root / reparse_name).exists())

        real_directory = self.root / "plain-directory"
        real_directory.mkdir()
        link = self.root / "reparse-directory"
        try:
            link.symlink_to(real_directory, target_is_directory=True)
        except OSError:
            with patch.object(
                acceptance,
                "is_reparse_point",
                side_effect=lambda path: Path(path) == real_directory,
            ), self.assertRaisesRegex(
                acceptance.V10FRealAgentAcceptanceError,
                "acceptance_output_reparse_denied",
            ):
                acceptance._assert_plain_directory(
                    real_directory,
                    reason_code="acceptance_output_reparse_denied",
                )
        else:
            with self.assertRaisesRegex(
                acceptance.V10FRealAgentAcceptanceError,
                "acceptance_output_reparse_denied",
            ):
                acceptance._assert_plain_directory(
                    link,
                    reason_code="acceptance_output_reparse_denied",
                )

    def test_windows_directory_pin_uses_minimal_access_without_delete_sharing(
        self,
    ) -> None:
        self.assertEqual(0x00000080, acceptance._WINDOWS_DIRECTORY_DESIRED_ACCESS)
        self.assertEqual(0x00000003, acceptance._WINDOWS_DIRECTORY_SHARE_MODE)
        source = inspect.getsource(
            acceptance._DirectoryHandlePins._open_windows_directory
        )
        self.assertIn("_WINDOWS_DIRECTORY_DESIRED_ACCESS", source)
        self.assertIn("_WINDOWS_DIRECTORY_SHARE_MODE", source)
        self.assertNotIn("0x00010000", source)

    def test_windows_directory_pin_opens_project_root_with_service_like_cwd(
        self,
    ) -> None:
        if os.name != "nt":
            self.skipTest("Windows directory sharing contract")
        helper = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-c",
                "import sys; sys.stdin.buffer.read(1)",
            ],
            cwd=acceptance.PROJECT_ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            self.assertIsNone(helper.poll())
            pins = acceptance._DirectoryHandlePins()
            try:
                pins.pin(acceptance.PROJECT_ROOT)
                pins.assert_path(acceptance.PROJECT_ROOT)
            finally:
                pins.close()
        finally:
            if helper.stdin is not None:
                try:
                    helper.stdin.write(b"x")
                    helper.stdin.close()
                except OSError:
                    pass
            try:
                helper.wait(timeout=5)
            except subprocess.TimeoutExpired:
                helper.kill()
                helper.wait(timeout=5)
            if helper.stderr is not None:
                helper.stderr.close()
        self.assertEqual(0, helper.returncode)

    def test_directory_handles_deny_or_detect_real_output_swap_without_escape(
        self,
    ) -> None:
        anchor = self.root / "handle-pinned-anchor"
        anchor.mkdir()
        output = anchor / "acceptance-output"
        external = self.root / "external-swap-target"
        external.mkdir()
        escaped_artifact = external / "must-not-exist.json"
        target = acceptance.AcceptanceTarget(
            operational_db=self.operational_db,
            runtime_db=self.runtime_db,
            output_root=output,
            production=False,
        )
        with acceptance._pinned_output_directory_handles(target) as pins:
            acceptance._create_pinned_output_root(
                target,
                directory_pins=pins,
            )
            pins.assert_path(target.output_root)
            moved = self.root / "moved-pinned-output"
            if os.name == "nt":
                acceptance._write_pinned_output_exclusive(
                    target=target,
                    name=acceptance.SENTRY_NAME,
                    payload={"state": "guard-ready"},
                )
                guard = acceptance._RetainedArtifactGuard(
                    target.output_root / acceptance.SENTRY_NAME
                )
                try:
                    with self.assertRaises(OSError):
                        target.output_root.rename(moved)
                    with self.assertRaises(OSError):
                        anchor.rename(self.root / "moved-pinned-anchor")
                    self.assertTrue(target.output_root.is_dir())
                    guard.assert_current()
                    pins.assert_all()
                finally:
                    guard.close()
            else:
                target.output_root.rename(moved)
                target.output_root.symlink_to(external, target_is_directory=True)
                with self.assertRaisesRegex(
                    acceptance.V10FRealAgentAcceptanceError,
                    "acceptance_output_root_identity_changed|"
                    "acceptance_output_reparse_denied|"
                    "acceptance_directory_handle_path_changed",
                ):
                    acceptance._write_pinned_output_exclusive(
                        target=target,
                        name=escaped_artifact.name,
                        payload={"must_not_escape": True},
                    )
        self.assertFalse(escaped_artifact.exists())

    def test_trusted_consent_rejects_the_pre_disclosure_user_utterance(self) -> None:
        self.assertEqual(
            "我知情并批准上述 DeepSeek 外发边界。",
            acceptance.USER_CONSENT_UTTERANCE,
        )
        self.assertEqual(
            hashlib.sha256(
                acceptance.USER_CONSENT_UTTERANCE.encode("utf-8")
            ).hexdigest(),
            acceptance.USER_CONSENT_TEXT_SHA256,
        )
        binding = {
            "confirmation_digest": self.confirmation.binding_digest,
            "objective_id": self.contracts.objective.objective_id,
            "objective_digest": self.contracts.objective.canonical_digest,
            "run_id": "run:test-v10-f-consent",
            "maximum_model_calls": 2,
            "do_not_rerun": True,
        }
        ledger = acceptance.TrustedConsentLedger(
            confirmation=self.confirmation,
            objective_id=self.contracts.objective.objective_id,
            objective_digest=self.contracts.objective.canonical_digest,
            controller_binding=binding,
        )
        with self.assertRaisesRegex(
            Exception,
            "trusted_user_event_controller_binding_mismatch",
        ):
            acceptance.create_trusted_user_event(
                controller=ledger.controller,
                event_id="trusteduserevent:test-v10-f-tampered",
                user_utterance="授权，继续",
                runtime_disclosure_digest="a" * 64,
                confirmation_binding_digest=ledger.controller_binding_digest,
                intended_consent_id="consent:test-v10-f-tampered",
                sequence=1,
                observed_at=acceptance._utc_now(),
            )
        with self.assertRaisesRegex(
            acceptance.V10FRealAgentAcceptanceError,
            "mock_acceptance_requires_system_temp",
        ):
            acceptance.AcceptanceTarget(
                operational_db=acceptance.PROJECT_ROOT / "not-a-test.db",
                runtime_db=self.runtime_db,
                output_root=self.output_root,
                production=False,
            )


if __name__ == "__main__":
    unittest.main()
