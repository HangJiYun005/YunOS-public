from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
import sqlite3
import unittest
from unittest.mock import patch

from memory.cognitive_capability import build_suggested_capability_registry
from memory.cognitive_orchestration import (
    CognitiveContext,
    CognitiveModelRequest,
    CognitiveOrchestrationError,
    ModelBudget,
    ModelBudgetState,
    STRUCTURED_OUTPUT_CONTRACT_VERSION,
    estimate_tokens,
    reserve_model_call,
)
from memory.cognitive_runtime_bridge import (
    MAX_BOUNDED_MODEL_CALLS,
    CognitiveRuntimeBridgeError,
    CognitiveRuntimeRecoveryRequired,
    PRODUCTION_FIXED_OUTPUT_TOKENS,
)
from memory.deepseek_v4_adapter import (
    DeepSeekV4ProviderError,
    DeepSeekV4Response,
)
from memory.production_runtime_execution import ProductionRuntimeExecutionError
from memory.runtime_protocol_contracts import (
    ARTIFACT_VERSION,
    BOUNDED_TOOL_ARTIFACT_VERSION,
    canonical_digest,
    canonical_json,
    validate_runtime_ledger,
)
from tests.test_v10_runtime_bridge import CognitiveRuntimeBridgeFixture


class V10DRuntimeBridgeFixture(CognitiveRuntimeBridgeFixture):
    def setUp(self) -> None:
        super().setUp()
        self.run_budget = ModelBudget(
            maximum_model_calls=2,
            maximum_input_tokens=129_024,
            maximum_output_tokens=2 * PRODUCTION_FIXED_OUTPUT_TOKENS,
            maximum_total_tokens=131_072,
            maximum_cost_microunits=2_000_000,
        )
        self.capability_registry = build_suggested_capability_registry(
            {"project.file.read": "available"}
        )

    def make_d_request(
        self,
        call_number: int,
        *,
        agency_level: str = "act",
        output_kind: str = "tool_proposal",
        budget: ModelBudget | None = None,
    ) -> CognitiveModelRequest:
        return CognitiveModelRequest(
            objective={
                "objective_id": self.objective_id,
                "objective_digest": self.objective_digest,
                "interpreted_goal": (
                    "Produce one bounded structured proposal for the current "
                    f"YunOS plan node, model call {call_number}."
                ),
                "agency_level": agency_level,
                "cognitive_stance": "continue",
                "model_call_sequence": call_number,
            },
            context=self.context,
            provider_binding=self.provider_binding,
            model_budget=budget or self.run_budget,
            expected_output_kind=output_kind,
            maximum_output_bytes=64_000,
        )

    def reserve(
        self,
        request: CognitiveModelRequest,
        state: ModelBudgetState = ModelBudgetState(),
    ):
        return reserve_model_call(
            mode="ai",
            request_digest=request.request_digest,
            provider_binding=request.provider_binding,
            estimated_input_tokens=64_512,
            requested_output_tokens=PRODUCTION_FIXED_OUTPUT_TOKENS,
            estimated_cost_microunits=1_000_000,
            provider_context_limit_tokens=65_536,
            budget=request.model_budget,
            state=state,
        )

    def prepare_call(
        self,
        bridge,
        request: CognitiveModelRequest,
        reservation,
        budget_state: ModelBudgetState,
        call_number: int,
    ):
        disclosure = bridge.prepare_disclosure(
            self.selection,
            request,
            reservation,
            request_id=f"request:v10-d-runtime-{call_number:04d}",
            mutation_id=f"mutation:v10-d-runtime-{call_number:04d}",
            budget_state=budget_state,
        )
        consent = self.make_consent(
            self.selection,
            request,
            reservation,
            disclosure,
        )
        preview = bridge.preview(
            disclosure,
            self.selection,
            request,
            reservation,
            consent=consent,
            budget_state=budget_state,
        )
        return consent, preview

    def execute_call(
        self,
        bridge,
        preview,
        request,
        reservation,
        consent,
        budget_state,
    ):
        return bridge.execute(
            preview,
            self.selection,
            request,
            reservation,
            consent=consent,
            confirmed=True,
            authority=bridge.execution_session,
            budget_state=budget_state,
        )

    def tool_response(self, request: CognitiveModelRequest) -> str:
        profile = self.capability_registry.lookup(
            "project.file.read",
            version=1,
        )
        return canonical_json(
            {
                "contract_version": STRUCTURED_OUTPUT_CONTRACT_VERSION,
                "request_digest": request.request_digest,
                "provider_binding": (
                    request.provider_binding.response_identity()
                ),
                "output_kind": "tool_proposal",
                "payload": {
                    "capability_id": profile.capability_id,
                    "capability_version": profile.version,
                    "capability_digest": profile.capability_digest,
                    "typed_arguments": {"relative_path": "README.md"},
                    "reason": "Read one bounded, authorized project file.",
                    "expected_observation": (
                        "Return bounded text and its content digest."
                    ),
                    "risk_class": profile.risk_class,
                    "proposal_only": True,
                    "authorization_claimed": False,
                    "execution_claimed": False,
                },
            }
        )

    def mock_response_patch(
        self,
        content: str,
        *,
        response_suffix: str,
    ):
        def send(_adapter, _prepared):
            return DeepSeekV4Response(
                provider_response_id=f"mock-v10-d-{response_suffix}",
                model=self.selection.model,
                content=content,
                usage={
                    "prompt_tokens": 96,
                    "completion_tokens": 24,
                    "total_tokens": 120,
                },
                finish_reason="stop",
            )

        return patch(
            "memory.production_runtime_execution._VisibleMockAdapter.send",
            new=send,
        )


class V10DBoundedRuntimeContractTest(V10DRuntimeBridgeFixture):
    def _bounded_blueprint_request(self) -> tuple[CognitiveModelRequest, dict]:
        profile = self.capability_registry.lookup(
            "project.file.read",
            version=1,
        )
        blueprint = {
            "capability_id": profile.capability_id,
            "capability_version": profile.version,
            "capability_digest": profile.capability_digest,
            "typed_arguments": {"relative_path": "README.md"},
            "reason": "Read only the authorized disposable file.",
            "expected_observation": "Return its content digest.",
            "risk_class": profile.risk_class,
        }
        model_context = deepcopy(self.context.to_dict()["model_context"])
        model_context["bounded_plan_node"] = {
            "node": {
                "capability_requirements": [
                    {
                        "capability_id": profile.capability_id,
                        "capability_version": profile.version,
                        "capability_digest": profile.capability_digest,
                    }
                ],
                "risk": profile.risk_class,
            }
        }
        model_context["bounded_tool_proposal_blueprint"] = blueprint
        encoded = canonical_json(model_context)
        context = CognitiveContext(
            objective_id=self.context.objective_id,
            objective_digest=self.context.objective_digest,
            bindings=self.context.bindings,
            model_context=model_context,
            budget=self.context.budget,
            selected_items=self.context.selected_items,
            canonical_bytes=len(encoded.encode("utf-8")),
            estimated_tokens=estimate_tokens(encoded),
        )
        request = CognitiveModelRequest(
            objective={
                "objective_id": self.objective_id,
                "objective_digest": self.objective_digest,
                "interpreted_goal": "Read one bounded sandbox file.",
                "agency_level": "act",
                "cognitive_stance": "continue",
            },
            context=context,
            provider_binding=self.provider_binding,
            model_budget=self.run_budget,
            expected_output_kind="tool_proposal",
            maximum_output_bytes=64_000,
        )
        return request, blueprint

    def test_typed_bounded_tool_keeps_short_ycd2_and_adds_sealed_binding(self) -> None:
        request, blueprint = self._bounded_blueprint_request()
        reservation, state = self.reserve(request)
        bridge = self.bridge()
        _consent, preview = self.prepare_call(
            bridge,
            request,
            reservation,
            state,
            1,
        )

        self.assertTrue(preview.question.startswith("YCD2|"))
        self.assertLessEqual(len(preview.question), 500)
        self.assertEqual("Medium", preview.budget_profile)
        binding = dict(preview.bounded_tool_request_binding or {})
        self.assertTrue(binding["request_identity"].startswith("btr_"))
        self.assertEqual(preview.question, binding["retrieval_question"])
        self.assertEqual(request.request_digest, binding["cognitive_request_digest"])
        self.assertEqual(
            blueprint["capability_digest"],
            binding["capability_digest"],
        )
        reviewed_limits = preview.runtime_reviewed_execution_binding[
            "reviewed_artifact"
        ]["budget"]["limits"]
        for key, value in binding.items():
            self.assertEqual(value, reviewed_limits["bounded_tool_binding"][key])

        legacy = self.make_d_request(2)
        legacy_reservation, legacy_state = self.reserve(legacy)
        _legacy_consent, legacy_preview = self.prepare_call(
            self.bridge(),
            legacy,
            legacy_reservation,
            legacy_state,
            2,
        )
        self.assertTrue(legacy_preview.question.startswith("YCD2|"))
        self.assertLessEqual(len(legacy_preview.question), 500)
        self.assertEqual("Small", legacy_preview.budget_profile)
        self.assertIsNone(legacy_preview.bounded_tool_request_binding)
        self.assertNotIn(
            "bounded_tool_request_binding",
            legacy_preview.identity_payload(),
        )

    def test_typed_bounded_tool_rejects_unbound_blueprint(self) -> None:
        request, _blueprint = self._bounded_blueprint_request()
        context_payload = deepcopy(request.context.to_dict()["model_context"])
        context_payload["bounded_tool_proposal_blueprint"][
            "capability_digest"
        ] = "f" * 64
        encoded = canonical_json(context_payload)
        context = CognitiveContext(
            objective_id=request.context.objective_id,
            objective_digest=request.context.objective_digest,
            bindings=request.context.bindings,
            model_context=context_payload,
            budget=request.context.budget,
            selected_items=request.context.selected_items,
            canonical_bytes=len(encoded.encode("utf-8")),
            estimated_tokens=estimate_tokens(encoded),
        )
        tampered = replace(request, context=context, request_digest="")
        reservation, state = self.reserve(tampered)
        bridge = self.bridge()
        with self.assertRaisesRegex(
            CognitiveRuntimeBridgeError,
            "bounded_tool_blueprint_capability_mismatch",
        ):
            bridge.prepare_disclosure(
                self.selection,
                tampered,
                reservation,
                request_id="request:v10-f-ycd3-mismatch",
                mutation_id="mutation:v10-f-ycd3-mismatch",
                budget_state=state,
            )
        self.assertEqual(0, bridge.diagnostics["preview_calls"])

    def test_typed_bounded_tool_over_budget_fails_before_runtime_preview(self) -> None:
        request, _blueprint = self._bounded_blueprint_request()
        context_payload = deepcopy(request.context.to_dict()["model_context"])
        context_payload["bounded_tool_proposal_blueprint"]["reason"] = (
            "bounded-" + "x" * 4_096
        )
        encoded = canonical_json(context_payload)
        context = CognitiveContext(
            objective_id=request.context.objective_id,
            objective_digest=request.context.objective_digest,
            bindings=request.context.bindings,
            model_context=context_payload,
            budget=request.context.budget,
            selected_items=request.context.selected_items,
            canonical_bytes=len(encoded.encode("utf-8")),
            estimated_tokens=estimate_tokens(encoded),
        )
        oversized = replace(request, context=context, request_digest="")
        reservation, state = self.reserve(oversized)
        bridge = self.bridge()
        with self.assertRaisesRegex(
            CognitiveRuntimeBridgeError,
            "bounded_tool_appendix_invalid",
        ):
            bridge.prepare_disclosure(
                self.selection,
                oversized,
                reservation,
                request_id="request:v10-f-ycd3-over-budget",
                mutation_id="mutation:v10-f-ycd3-over-budget",
                budget_state=state,
            )
        self.assertEqual(0, bridge.diagnostics["preview_calls"])

    def test_typed_and_legacy_runs_share_and_validate_one_v8_ledger(self) -> None:
        bounded_request, _blueprint = self._bounded_blueprint_request()
        bounded_reservation, bounded_state = self.reserve(bounded_request)
        bridge = self.bridge()
        bounded_consent, bounded_preview = self.prepare_call(
            bridge,
            bounded_request,
            bounded_reservation,
            bounded_state,
            11,
        )
        bounded_execution = self.execute_call(
            bridge,
            bounded_preview,
            bounded_request,
            bounded_reservation,
            bounded_consent,
            bounded_state,
        )
        self.assertEqual("succeeded", bounded_execution.status)

        legacy_request = self.make_d_request(12)
        legacy_reservation, legacy_state = self.reserve(legacy_request)
        legacy_consent, legacy_preview = self.prepare_call(
            bridge,
            legacy_request,
            legacy_reservation,
            legacy_state,
            12,
        )
        legacy_execution = self.execute_call(
            bridge,
            legacy_preview,
            legacy_request,
            legacy_reservation,
            legacy_consent,
            legacy_state,
        )
        self.assertEqual("succeeded", legacy_execution.status)

        tables = (
            "runtime_conversations",
            "runtime_request_artifacts",
            "runtime_model_runs",
            "runtime_messages",
            "runtime_run_events",
            "runtime_errors",
            "runtime_receipts",
        )
        with sqlite3.connect(self.sidecar) as connection:
            connection.row_factory = sqlite3.Row
            snapshot = {
                table: [
                    dict(row)
                    for row in connection.execute(f"SELECT * FROM {table}")
                ]
                for table in tables
            }
            artifact_versions = {
                json.loads(row["artifact_json"])["artifact_version"]
                for row in snapshot["runtime_request_artifacts"]
            }
        validated = validate_runtime_ledger(snapshot)
        self.assertEqual(
            {ARTIFACT_VERSION, BOUNDED_TOOL_ARTIFACT_VERSION},
            artifact_versions,
        )
        self.assertEqual(2, len(validated["run_states"]))
        self.assertTrue(
            all(
                state == "succeeded"
                for state in validated["run_states"].values()
            )
        )

    def test_plan_and_act_structured_preview_require_run_budget_state(self) -> None:
        plan_request = self.make_d_request(
            1,
            agency_level="plan",
            output_kind="plan",
        )
        plan_reservation, plan_state = self.reserve(plan_request)
        bridge = self.bridge()
        with self.assertRaisesRegex(
            CognitiveRuntimeBridgeError,
            "one_model_call_budget_required",
        ):
            bridge.prepare_disclosure(
                self.selection,
                plan_request,
                plan_reservation,
                request_id="request:v10-d-no-state-0001",
                mutation_id="mutation:v10-d-no-state-0001",
            )
        self.assertEqual(0, bridge.diagnostics["preview_calls"])
        _, plan_preview = self.prepare_call(
            bridge,
            plan_request,
            plan_reservation,
            plan_state,
            1,
        )
        self.assertTrue(plan_preview.question.startswith("YCD2|"))
        self.assertIn("kind=plan", plan_preview.question)
        self.assertIn(plan_request.request_digest, plan_preview.question)
        self.assertIn(
            self.selection.profile_digest,
            plan_preview.question,
        )
        self.assertLessEqual(len(plan_preview.question), 500)

        act_request = self.make_d_request(2)
        act_reservation, act_state = self.reserve(
            act_request,
            plan_state,
        )
        _, act_preview = self.prepare_call(
            bridge,
            act_request,
            act_reservation,
            act_state,
            2,
        )
        self.assertIn("kind=tool_proposal", act_preview.question)
        self.assertFalse(act_request.grants_authority)
        self.assertFalse(act_request.execution_allowed)

    def test_tool_proposal_reads_v8_observation_and_settles_run_budget(self) -> None:
        request = self.make_d_request(1)
        reservation, state = self.reserve(request)
        bridge = self.bridge()
        consent, preview = self.prepare_call(
            bridge,
            request,
            reservation,
            state,
            1,
        )
        with self.mock_response_patch(
            self.tool_response(request),
            response_suffix="tool-1",
        ):
            execution = self.execute_call(
                bridge,
                preview,
                request,
                reservation,
                consent,
                state,
            )
        self.assertEqual("succeeded", execution.status)
        self.assertEqual(1, execution.adapter_call_count_this_request)
        self.assertEqual(1, execution.model_call_count_for_run)
        self.assertTrue(execution.exactly_one_call)
        self.assertFalse(execution.automatic_retry)
        self.assertFalse(self.selection.fallback_used)

        observation = bridge.read_model_observation(
            preview,
            request,
            reservation,
            budget_state=state,
        )
        self.assertEqual("succeeded", observation.status)
        self.assertEqual("untrusted_model_output", observation.trust_class)
        self.assertEqual(
            canonical_digest(observation.identity_payload()),
            observation.observation_digest,
        )
        self.assertFalse(
            observation.to_dict()["response_content_serialized"]
        )
        self.assertNotIn(
            "README.md",
            canonical_json(observation.to_dict()),
        )

        reconciled = bridge.reconcile_model_run(
            preview,
            request,
            reservation,
            budget_state=state,
            capability_registry=self.capability_registry,
        )
        self.assertEqual("reconciled_succeeded", reconciled.state)
        self.assertEqual("tool_proposal", reconciled.parsed_output.output_kind)
        self.assertEqual(
            "validated_proposal_only",
            reconciled.parsed_output.payload["registry_decision"],
        )
        self.assertFalse(reconciled.parsed_output.execution_allowed)
        self.assertFalse(reconciled.grants_authority)
        self.assertFalse(reconciled.execution_allowed)
        self.assertFalse(reconciled.system_verified)
        self.assertFalse(reconciled.user_accepted)
        self.assertIn(
            reservation.reservation_id,
            reconciled.budget_state.settled_reservation_ids,
        )
        self.assertEqual(96, reconciled.budget_state.actual_input_tokens)
        self.assertEqual(24, reconciled.budget_state.actual_output_tokens)
        self.assertEqual(
            reservation.cost_microunits_ceiling,
            reconciled.budget_state.unknown_cost_reserved_ceiling_microunits,
        )
        self.assertEqual(1, bridge.diagnostics["execute_calls"])
        self.assertEqual(1, self.ledger_counts()["runtime_model_runs"])
        self.assertEqual(1, self.ledger_counts()["runtime_receipts"])

        with self.assertRaisesRegex(
            CognitiveRuntimeBridgeError,
            "model_run_reconciliation_replay",
        ):
            bridge.reconcile_model_run(
                preview,
                request,
                reservation,
                budget_state=reconciled.budget_state,
                capability_registry=self.capability_registry,
            )

        with self.assertRaisesRegex(
            CognitiveRuntimeBridgeError,
            "cognitive_runtime_observation_factory_required",
        ):
            replace(observation, grants_authority=True)
        with self.assertRaisesRegex(
            CognitiveRuntimeBridgeError,
            "cognitive_runtime_reconciliation_factory_required",
        ):
            replace(reconciled, user_accepted=True)

    def test_two_model_calls_share_one_bounded_state_but_each_send_once(self) -> None:
        bridge = self.bridge()
        state = ModelBudgetState()
        executions = []
        reconciliations = []
        for call_number in (1, 2):
            request = self.make_d_request(call_number)
            reservation, state = self.reserve(request, state)
            consent, preview = self.prepare_call(
                bridge,
                request,
                reservation,
                state,
                call_number,
            )
            with self.mock_response_patch(
                self.tool_response(request),
                response_suffix=f"tool-{call_number}",
            ):
                execution = self.execute_call(
                    bridge,
                    preview,
                    request,
                    reservation,
                    consent,
                    state,
                )
            reconciliation = bridge.reconcile_model_run(
                preview,
                request,
                reservation,
                budget_state=state,
                capability_registry=self.capability_registry,
            )
            state = reconciliation.budget_state
            executions.append(execution)
            reconciliations.append(reconciliation)

        self.assertEqual(2, state.calls_reserved)
        self.assertEqual(2, len(state.settled_reservation_ids))
        self.assertEqual(192, state.actual_input_tokens)
        self.assertEqual(48, state.actual_output_tokens)
        self.assertEqual(2_000_000, state.unknown_cost_reserved_ceiling_microunits)
        self.assertTrue(all(item.exactly_one_call for item in executions))
        self.assertTrue(
            all(item.adapter_call_count_this_request == 1 for item in executions)
        )
        self.assertTrue(
            all(item.state == "reconciled_succeeded" for item in reconciliations)
        )
        self.assertEqual(2, bridge.diagnostics["execute_calls"])
        self.assertFalse(bridge.diagnostics["automatic_retry"])
        self.assertEqual(2, self.ledger_counts()["runtime_model_runs"])
        self.assertEqual(2, self.ledger_counts()["runtime_receipts"])

        third_request = self.make_d_request(3)
        with self.assertRaisesRegex(
            CognitiveOrchestrationError,
            "model_call_budget_exhausted",
        ):
            self.reserve(third_request, state)

    def test_invalid_structured_output_is_rejected_after_budget_settlement(
        self,
    ) -> None:
        request = self.make_d_request(1)
        reservation, state = self.reserve(request)
        bridge = self.bridge()
        consent, preview = self.prepare_call(
            bridge,
            request,
            reservation,
            state,
            1,
        )
        with self.mock_response_patch(
            "not-json",
            response_suffix="invalid-json",
        ):
            self.execute_call(
                bridge,
                preview,
                request,
                reservation,
                consent,
                state,
            )
        reconciled = bridge.reconcile_model_run(
            preview,
            request,
            reservation,
            budget_state=state,
            capability_registry=self.capability_registry,
        )
        self.assertEqual("rejected_model_output", reconciled.state)
        self.assertIsNone(reconciled.parsed_output)
        self.assertIn("structured_output", reconciled.reason_code)
        self.assertIn(
            reservation.reservation_id,
            reconciled.budget_state.settled_reservation_ids,
        )
        self.assertEqual(1, bridge.diagnostics["execute_calls"])

    def test_terminal_provider_failure_settles_conservative_ceiling_no_retry(
        self,
    ) -> None:
        request = self.make_d_request(1)
        reservation, state = self.reserve(request)
        bridge = self.bridge()
        consent, preview = self.prepare_call(
            bridge,
            request,
            reservation,
            state,
            1,
        )

        def fail(_adapter, _prepared):
            raise DeepSeekV4ProviderError(
                "mock_bounded_provider_failure",
                category="provider",
                retryable=True,
            )

        with patch(
            "memory.production_runtime_execution._VisibleMockAdapter.send",
            new=fail,
        ):
            execution = self.execute_call(
                bridge,
                preview,
                request,
                reservation,
                consent,
                state,
            )
        self.assertEqual("failed", execution.status)
        self.assertEqual(1, execution.adapter_call_count_this_request)
        self.assertFalse(execution.automatic_retry)
        reconciled = bridge.reconcile_model_run(
            preview,
            request,
            reservation,
            budget_state=state,
            capability_registry=self.capability_registry,
        )
        self.assertEqual("reconciled_failed", reconciled.state)
        self.assertEqual("mock_bounded_provider_failure", reconciled.reason_code)
        self.assertEqual(
            reservation.input_tokens,
            reconciled.budget_state.actual_input_tokens,
        )
        self.assertEqual(
            reservation.output_token_ceiling,
            reconciled.budget_state.actual_output_tokens,
        )
        self.assertEqual(
            reservation.cost_microunits_ceiling,
            reconciled.budget_state.unknown_cost_reserved_ceiling_microunits,
        )
        self.assertEqual(1, bridge.diagnostics["execute_calls"])

    def test_nonterminal_and_in_doubt_runs_require_recovery(self) -> None:
        request = self.make_d_request(1)
        reservation, state = self.reserve(request)
        bridge = self.bridge()
        consent, preview = self.prepare_call(
            bridge,
            request,
            reservation,
            state,
            1,
        )
        with self.mock_response_patch(
            self.tool_response(request),
            response_suffix="recovery-base",
        ):
            self.execute_call(
                bridge,
                preview,
                request,
                reservation,
                consent,
                state,
            )
        authoritative = bridge._runtime.get_run(preview.runtime_model_run_id)
        cases = (
            ("prepared", "not_attempted"),
            ("running", "not_attempted"),
            ("running", "send_attempted_result_unknown"),
            ("running", "provider_response_received_local_commit_pending"),
            ("failed", "provider_response_persist_in_doubt"),
            ("failed", "send_attempt_in_doubt"),
        )
        for status, phase in cases:
            changed = deepcopy(authoritative)
            changed["status"] = status
            changed["execution_phase"] = phase
            with self.subTest(status=status, phase=phase), patch.object(
                bridge._runtime,
                "get_run",
                return_value=changed,
            ):
                with self.assertRaises(
                    CognitiveRuntimeRecoveryRequired
                ) as captured:
                    bridge.read_model_observation(
                        preview,
                        request,
                        reservation,
                        budget_state=state,
                    )
                self.assertEqual("recovery_required", captured.exception.state)
                self.assertEqual(phase, captured.exception.execution_phase)
                self.assertFalse(captured.exception.grants_authority)
                self.assertFalse(captured.exception.execution_allowed)
        self.assertEqual(1, bridge.diagnostics["execute_calls"])

    def test_uncertain_ledger_read_is_recovery_required(self) -> None:
        request = self.make_d_request(1)
        reservation, state = self.reserve(request)
        bridge = self.bridge()
        _, preview = self.prepare_call(
            bridge,
            request,
            reservation,
            state,
            1,
        )
        with self.assertRaises(
            CognitiveRuntimeRecoveryRequired
        ) as not_persisted:
            bridge.read_model_observation(
                preview,
                request,
                reservation,
                budget_state=state,
            )
        self.assertEqual(
            "not_persisted",
            not_persisted.exception.execution_phase,
        )
        self.assertEqual(
            "model_run_not_found",
            not_persisted.exception.cause_reason,
        )
        with patch.object(
            bridge._runtime,
            "get_run",
            side_effect=ProductionRuntimeExecutionError(
                "ledger_binding_failure"
            ),
        ):
            with self.assertRaises(
                CognitiveRuntimeRecoveryRequired
            ) as captured:
                bridge.read_model_observation(
                    preview,
                    request,
                    reservation,
                    budget_state=state,
                )
        self.assertEqual("recovery_required", captured.exception.state)
        self.assertEqual("runtime_read_failed", captured.exception.execution_phase)
        self.assertEqual(
            "ledger_binding_failure",
            captured.exception.cause_reason,
        )
        self.assertEqual(0, bridge.diagnostics["execute_calls"])

    def test_bridge_cap_and_budget_state_binding_fail_closed_before_preview(
        self,
    ) -> None:
        over_cap = ModelBudget(
            maximum_model_calls=MAX_BOUNDED_MODEL_CALLS + 1,
            maximum_input_tokens=64_512,
            maximum_output_tokens=PRODUCTION_FIXED_OUTPUT_TOKENS,
            maximum_total_tokens=65_536,
            maximum_cost_microunits=1_000_000,
        )
        request = self.make_d_request(1, budget=over_cap)
        reservation, state = self.reserve(request)
        bridge = self.bridge()
        with self.assertRaisesRegex(
            CognitiveRuntimeBridgeError,
            "bounded_model_call_budget_required",
        ):
            bridge.prepare_disclosure(
                self.selection,
                request,
                reservation,
                request_id="request:v10-d-over-cap-0001",
                mutation_id="mutation:v10-d-over-cap-0001",
                budget_state=state,
            )
        self.assertEqual(0, bridge.diagnostics["preview_calls"])

        normal_request = self.make_d_request(1)
        normal_reservation, normal_state = self.reserve(normal_request)
        mismatched = replace(
            normal_state,
            request_digests=("f" * 64,),
        )
        with self.assertRaisesRegex(
            CognitiveRuntimeBridgeError,
            "model_budget_state_binding_mismatch",
        ):
            bridge.prepare_disclosure(
                self.selection,
                normal_request,
                normal_reservation,
                request_id="request:v10-d-bad-state-0001",
                mutation_id="mutation:v10-d-bad-state-0001",
                budget_state=mismatched,
            )
        self.assertEqual(0, bridge.diagnostics["preview_calls"])

        forged_reservation = replace(
            normal_reservation,
            reservation_id="modelres:forged-v10-d-reservation",
        )
        with self.assertRaisesRegex(
            CognitiveRuntimeBridgeError,
            "model_budget_state_binding_mismatch",
        ):
            bridge.prepare_disclosure(
                self.selection,
                normal_request,
                forged_reservation,
                request_id="request:v10-d-forged-reservation-0001",
                mutation_id="mutation:v10-d-forged-reservation-0001",
                budget_state=normal_state,
            )
        self.assertEqual(0, bridge.diagnostics["preview_calls"])


if __name__ == "__main__":
    unittest.main()
