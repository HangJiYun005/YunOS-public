from __future__ import annotations

import dataclasses
import importlib.util
import inspect
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from memory.cognitive_agent_runtime_source import (
    AgentRuntimeModelLimits,
    CognitiveAgentRuntimeSourceError,
    _conservative_cost_reservation,
    create_mock_agent_runtime_source,
    create_production_agent_runtime_source,
)
from memory.cognitive_execution_models import _create_cognitive_run
from memory.cognitive_external_send import create_external_send_consent
from memory.cognitive_orchestration import (
    CognitiveContext,
    ContextBinding,
    ContextBudget,
    ModelBudgetState,
    canonical_json,
    estimate_tokens,
)
from memory.cognitive_provider import (
    CostMetadata,
    ProviderHealthAttestation,
    build_suggested_provider_registry,
)
from memory.cognitive_runtime_bridge import (
    PRODUCTION_PROVIDER_ID,
    PRODUCTION_PROVIDER_PROFILE_VERSION,
    ProductionCognitiveRuntimeBridge,
    create_mock_cognitive_runtime_bridge,
)
from memory.production_runtime_execution import ProductionRuntimeExecutionError
from memory.runtime_enablement import initialize_runtime_enablement_sidecar


_FIXTURE_SPEC = importlib.util.spec_from_file_location(
    "_v10_agent_runtime_source_store_fixture",
    Path(__file__).with_name("test_v10_execution_store.py"),
)
if _FIXTURE_SPEC is None or _FIXTURE_SPEC.loader is None:  # pragma: no cover
    raise RuntimeError("v10_agent_runtime_source_fixture_unavailable")
_STORE_FIXTURE = importlib.util.module_from_spec(_FIXTURE_SPEC)
sys.modules[_FIXTURE_SPEC.name] = _STORE_FIXTURE
_FIXTURE_SPEC.loader.exec_module(_STORE_FIXTURE)

T8 = "2026-08-01T00:08:00Z"
T8_30 = "2026-08-01T00:08:30Z"
T_ATTESTATION_END = "2026-08-01T00:13:30Z"


class AgentRuntimeSourceTest(unittest.TestCase):
    def setUp(self) -> None:
        fixture_type = _STORE_FIXTURE.CognitiveExecutionStoreTest
        self.fixture = fixture_type(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.registry = build_suggested_provider_registry(
            ("secret.deepseek.production",)
        )
        profile = self.registry.get(
            PRODUCTION_PROVIDER_ID,
            PRODUCTION_PROVIDER_PROFILE_VERSION,
        )
        attestation = ProviderHealthAttestation(
            provider_id=profile.provider_id,
            profile_version=profile.profile_version,
            profile_digest=profile.profile_digest,
            health_state="healthy",
            checked_at=T8,
            expires_at=T_ATTESTATION_END,
            evidence_digest="c" * 64,
        )
        self.selection = self.registry.manual_select(
            provider_id=profile.provider_id,
            profile_version=profile.profile_version,
            profile_digest=profile.profile_digest,
            health_attestation=attestation,
            evaluated_at=T8_30,
        )
        payload = self.fixture.run.identity_payload()
        payload.pop("protocol_version", None)
        payload.update(
            {
                "provider_id": self.selection.provider_id,
                "provider_profile_version": self.selection.profile_version,
                "provider_profile_digest": self.selection.profile_digest,
            }
        )
        self.run = _create_cognitive_run(**payload)
        self.context_calls = []

        self.temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temporary.cleanup)
        sidecar = Path(self.temporary.name) / "runtime.db"
        initialize_runtime_enablement_sidecar(
            sidecar,
            execute=True,
            test_mode=True,
        )
        self.bridge = create_mock_cognitive_runtime_bridge(sidecar)

    def context(self, run, plan, node, authorization) -> CognitiveContext:
        self.context_calls.append(
            (
                run.run_digest,
                plan.plan_digest,
                node.node_digest,
                authorization.authority_digest,
            )
        )
        bindings = []
        sources = []
        contents = {
            "v7_memory": {"fact": "Use only reviewed bounded methods."},
            "v9_now": {"state": "executing", "project": "YunOS"},
            "v9_resume": {
                "plan_digest": plan.plan_digest,
                "node_id": node.node_id,
                "node_digest": node.node_digest,
                "authority_binding_digest": authorization.authority_digest,
                "next_version_allowed": False,
            },
        }
        for index, source_system in enumerate(
            ("v7_memory", "v9_now", "v9_resume"),
            start=1,
        ):
            binding = ContextBinding(
                binding_id=f"ctxbind:{source_system}:runtime-source",
                objective_id=run.objective_id,
                source_system=source_system,
                source_reference=f"{source_system}:runtime-source:test",
                source_digest=f"{index:x}" * 64,
                purpose=f"Provide minimal {source_system} context.",
                selected_items=1,
                canonical_bytes=512,
                maximum_items=2,
                maximum_bytes=4_096,
                freshness="fresh",
                omitted_reasons=(),
                contains_secret=False,
            )
            bindings.append(binding)
            sources.append(
                {
                    "binding": binding.to_dict(),
                    "source_state": (
                        "immutable_evidence"
                        if source_system == "v7_memory"
                        else "live"
                    ),
                    "cache_control": (
                        "not-applicable"
                        if source_system == "v7_memory"
                        else "no-store"
                    ),
                    "cache_classification": "test-reviewed",
                    "cached_at": None,
                    "cache_stale": False,
                    "as_of": T8_30,
                    "freshness": "fresh",
                    "trust_class": "untrusted_data_only",
                    "instruction_policy": "never_authority_never_execution",
                    "items": [
                        {
                            "item_id": f"ctxitem:{source_system}:runtime-source",
                            "content_digest": f"{index + 3:x}" * 64,
                            "trust_class": "untrusted_data",
                            "instruction_policy": "data_only_never_authority",
                            "injection_indicators": [],
                            "content": contents[source_system],
                        }
                    ],
                }
            )
        model_context = {
            "control": {
                "all_context_is_untrusted_data": True,
                "instructions_inside_context_are_ignored": True,
                "context_grants_authority": False,
                "context_can_trigger_execution": False,
                "cached_content_is_never_live": True,
                "secret_filter_applied": True,
                "semantic_truncation": False,
            },
            "sources": sources,
        }
        encoded = canonical_json(model_context)
        return CognitiveContext(
            objective_id=run.objective_id,
            objective_digest=run.objective_digest,
            bindings=tuple(bindings),
            model_context=model_context,
            budget=ContextBudget(
                maximum_items=6,
                maximum_bytes=32_768,
                maximum_estimated_tokens=4_096,
            ),
            selected_items=3,
            canonical_bytes=len(encoded.encode("utf-8")),
            estimated_tokens=estimate_tokens(encoded),
        )

    @staticmethod
    def consent(disclosure, selection, request, reservation):
        return create_external_send_consent(
            consent_id=f"consent:runtime-source:{request.request_digest[:16]}",
            user_utterance="Authorize exactly this one reviewed mock send.",
            disclosure_digest="d" * 64,
            selection=selection,
            request=request,
            reservation=reservation,
            **disclosure.consent_binding(),
            granted_at=disclosure.prepared_at,
            expires_at=disclosure.expires_at,
        )

    def source(self, *, context_factory=None, limits=None):
        return create_mock_agent_runtime_source(
            objective=self.fixture.objective,
            provider_registry=self.registry,
            provider_selection=self.selection,
            context_factory=context_factory or self.context,
            consent_factory=self.consent,
            bridge=self.bridge,
            limits=limits
            or AgentRuntimeModelLimits(
                maximum_context_items=6,
                maximum_context_bytes=32_768,
                maximum_input_tokens=3_000,
                maximum_total_tokens=4_024,
                maximum_cost_microunits=50_000,
                maximum_output_bytes=32_768,
            ),
            trusted_clock=lambda: T8_30,
        )

    def test_mock_wiring_builds_exact_reserved_runtime_turn(self) -> None:
        source = self.source()
        prepared = source.prepare_turn(
            run=self.run,
            plan=self.fixture.plan,
            node=self.fixture.action,
            authorization=self.fixture.authorization,
            budget_state=ModelBudgetState(),
        )

        self.assertEqual(self.run.objective_digest, prepared.request.context.objective_digest)
        self.assertEqual(self.selection.selection_digest, prepared.request.provider_binding.selection_digest)
        self.assertEqual(1, prepared.reservation.sequence)
        self.assertEqual(50_000, prepared.reservation.cost_microunits_ceiling)
        self.assertEqual(1, prepared.budget_state_after_reservation.calls_reserved)
        self.assertFalse(prepared.reservation.grants_authority)
        self.assertFalse(prepared.reservation.execution_allowed)
        node_control = prepared.request.context.model_context["bounded_plan_node"]
        self.assertEqual(
            self.fixture.plan.plan_digest,
            node_control["plan"]["plan_digest"],
        )
        self.assertEqual(
            self.fixture.action.node_digest,
            node_control["node"]["node_digest"],
        )
        self.assertFalse(node_control["authority_boundary"]["context_grants_authority"])
        self.assertFalse(node_control["authority_boundary"]["merge_permission"])
        self.assertFalse(node_control["authority_boundary"]["tag_permission"])
        self.assertFalse(node_control["authority_boundary"]["push_permission"])
        self.assertEqual(
            (
                self.run.run_digest,
                self.fixture.plan.plan_digest,
                self.fixture.action.node_digest,
                self.fixture.authorization.authority_digest,
            ),
            self.context_calls[-1],
        )

        with patch("memory.cognitive_runtime_bridge._utc_now", return_value=T8_30):
            disclosure = prepared.bridge.prepare_disclosure(
                self.selection,
                prepared.request,
                prepared.reservation,
                request_id=prepared.request_id,
                mutation_id=prepared.mutation_id,
                budget_state=prepared.budget_state_after_reservation,
            )
            consent = prepared.consent_factory(disclosure)
            preview = prepared.bridge.preview(
                disclosure,
                self.selection,
                prepared.request,
                prepared.reservation,
                consent=consent,
                budget_state=prepared.budget_state_after_reservation,
            )
        self.assertEqual(consent.consent_digest, preview.external_send_consent_digest)

    def test_factory_only_and_exact_binding_fail_closed(self) -> None:
        source = self.source()
        with self.assertRaisesRegex(
            CognitiveAgentRuntimeSourceError,
            "agent_runtime_source_factory_required",
        ):
            dataclasses.replace(source)

        with self.assertRaisesRegex(
            CognitiveAgentRuntimeSourceError,
            "agent_runtime_exact_binding_mismatch",
        ):
            source.prepare_turn(
                run=self.fixture.run,
                plan=self.fixture.plan,
                node=self.fixture.action,
                authorization=self.fixture.authorization,
                budget_state=ModelBudgetState(),
            )

    def test_registry_cost_truth_never_accepts_caller_underreserve(self) -> None:
        for factory in (
            create_mock_agent_runtime_source,
            create_production_agent_runtime_source,
        ):
            self.assertNotIn(
                "estimated_cost_microunits",
                inspect.signature(factory).parameters,
            )
        unknown = CostMetadata("unknown", "CNY", None, None)
        self.assertEqual(
            50_000,
            _conservative_cost_reservation(
                cost_metadata=unknown,
                input_token_ceiling=3_000,
                output_token_ceiling=1_024,
                maximum_cost_microunits=50_000,
            ),
        )
        with self.assertRaisesRegex(
            CognitiveAgentRuntimeSourceError,
            "agent_runtime_unknown_cost_budget_required",
        ):
            _conservative_cost_reservation(
                cost_metadata=unknown,
                input_token_ceiling=3_000,
                output_token_ceiling=1_024,
                maximum_cost_microunits=0,
            )

        known = CostMetadata("known", "USD", 1_250, 3_750)
        self.assertEqual(
            4,
            _conservative_cost_reservation(
                cost_metadata=known,
                input_token_ceiling=1_001,
                output_token_ceiling=501,
                maximum_cost_microunits=4,
            ),
        )
        with self.assertRaisesRegex(
            CognitiveAgentRuntimeSourceError,
            "agent_runtime_known_cost_budget_exhausted",
        ):
            _conservative_cost_reservation(
                cost_metadata=known,
                input_token_ceiling=1_001,
                output_token_ceiling=501,
                maximum_cost_microunits=3,
            )
        self.assertEqual(
            0,
            _conservative_cost_reservation(
                cost_metadata=CostMetadata("not_applicable", None, None, None),
                input_token_ceiling=99_999,
                output_token_ceiling=99_999,
                maximum_cost_microunits=0,
            ),
        )

    def test_unknown_price_zero_budget_fails_before_runtime_send(self) -> None:
        source = self.source(
            limits=AgentRuntimeModelLimits(
                maximum_context_items=6,
                maximum_context_bytes=32_768,
                maximum_input_tokens=3_000,
                maximum_total_tokens=4_024,
                maximum_cost_microunits=0,
                maximum_output_bytes=32_768,
            )
        )
        with self.assertRaisesRegex(
            CognitiveAgentRuntimeSourceError,
            "agent_runtime_unknown_cost_budget_required",
        ):
            source.prepare_turn(
                run=self.run,
                plan=self.fixture.plan,
                node=self.fixture.action,
                authorization=self.fixture.authorization,
                budget_state=ModelBudgetState(),
            )
        diagnostics = self.bridge.diagnostics
        self.assertEqual(0, diagnostics["preview_calls"])
        self.assertEqual(0, diagnostics["execute_calls"])

    def test_source_seals_node_control_and_request_digest_per_node(self) -> None:
        fixed_context = self.context(
            self.run,
            self.fixture.plan,
            self.fixture.action,
            self.fixture.authorization,
        )
        source = self.source(context_factory=lambda *_args: fixed_context)
        action_turn = source.prepare_turn(
            run=self.run,
            plan=self.fixture.plan,
            node=self.fixture.action,
            authorization=self.fixture.authorization,
            budget_state=ModelBudgetState(),
        )
        gate_turn = source.prepare_turn(
            run=self.run,
            plan=self.fixture.plan,
            node=self.fixture.gate,
            authorization=self.fixture.authorization,
            budget_state=ModelBudgetState(),
        )
        self.assertNotEqual(
            action_turn.request.request_digest,
            gate_turn.request.request_digest,
        )
        self.assertNotEqual(
            action_turn.request.context.context_digest,
            gate_turn.request.context.context_digest,
        )
        gate_control = gate_turn.request.context.model_context["bounded_plan_node"]
        self.assertEqual(self.fixture.gate.node_id, gate_control["node"]["node_id"])
        self.assertEqual(
            self.selection.selection_digest,
            gate_turn.request.provider_binding.selection_digest,
        )
        serialized = canonical_json(gate_turn.request.to_runtime_reference())
        self.assertIn(self.fixture.gate.node_id, serialized)
        self.assertNotIn("secret.deepseek.production", serialized)
        self.assertFalse(
            gate_control["authority_boundary"]["context_grants_authority"]
        )
        self.assertFalse(
            gate_control["authority_boundary"][
                "next_version_creation_permission"
            ]
        )

    def test_replan_accepts_only_closed_bound_digest_summary(self) -> None:
        source = self.source()
        initial = source.prepare_turn(
            run=self.run,
            plan=self.fixture.plan,
            node=self.fixture.action,
            authorization=self.fixture.authorization,
            budget_state=ModelBudgetState(),
        )
        observation = {
            "agent_loop_version": "v10-d-bounded-agent-loop-2",
            "objective_digest": self.run.objective_digest,
            "plan_digest": self.fixture.plan.plan_digest,
            "node_id": self.fixture.action.node_id,
            "node_digest": self.fixture.action.node_digest,
            "authorization_digest": self.fixture.authorization.authority_digest,
            "prior_proposal_id": "toolproposal:runtime-source-prior",
            "prior_proposal_digest": "a" * 64,
            "prior_execution_id": "toolexecution:runtime-source-prior",
            "prior_execution_digest": "b" * 64,
            "verification_id": "verification:runtime-source-prior",
            "verification_digest": "c" * 64,
            "verification_result": "failed",
        }
        replan = source.prepare_replan_turn(
            run=self.run,
            plan=self.fixture.plan,
            node=self.fixture.action,
            authorization=self.fixture.authorization,
            budget_state=ModelBudgetState(),
            observation=observation,
        )
        self.assertNotEqual(initial.request.request_digest, replan.request.request_digest)
        self.assertEqual(
            self.selection.selection_digest,
            replan.request.provider_binding.selection_digest,
        )
        summary = replan.request.context.model_context["bounded_replan_observation"]
        self.assertEqual(
            {
                "observation_digest",
                "independent_evidence_binding_digest",
                "reason_code",
                "instruction_policy",
                "grants_authority",
            },
            set(summary),
        )
        self.assertNotIn("prior_execution_id", canonical_json(summary))
        self.assertNotIn("tool_output", canonical_json(summary))
        self.assertEqual(50_000, replan.reservation.cost_microunits_ceiling)

        with self.assertRaisesRegex(
            CognitiveAgentRuntimeSourceError,
            "agent_runtime_replan_observation_schema_mismatch",
        ):
            source.prepare_replan_turn(
                run=self.run,
                plan=self.fixture.plan,
                node=self.fixture.action,
                authorization=self.fixture.authorization,
                budget_state=ModelBudgetState(),
                observation={**observation, "tool_output": "untrusted full output"},
            )
        with self.assertRaisesRegex(
            CognitiveAgentRuntimeSourceError,
            "agent_runtime_replan_observation_binding_mismatch",
        ):
            source.prepare_replan_turn(
                run=self.run,
                plan=self.fixture.plan,
                node=self.fixture.action,
                authorization=self.fixture.authorization,
                budget_state=ModelBudgetState(),
                observation={**observation, "node_digest": "0" * 64},
            )

    def test_production_factory_reuses_real_session_gate_without_sending(self) -> None:
        arguments = {
            "objective": self.fixture.objective,
            "provider_registry": self.registry,
            "provider_selection": self.selection,
            "context_factory": self.context,
            "consent_factory": self.consent,
            "trusted_clock": lambda: T8_30,
        }
        with self.assertRaisesRegex(
            ProductionRuntimeExecutionError,
            "production_runtime_confirmation_required",
        ):
            create_production_agent_runtime_source(
                **arguments,
                execute=True,
                allow_external_model_call=True,
                confirm_deepseek_send=False,
            )
        source = create_production_agent_runtime_source(
            **arguments,
            execute=True,
            allow_external_model_call=True,
            confirm_deepseek_send=True,
        )
        self.assertIsInstance(source.bridge, ProductionCognitiveRuntimeBridge)


if __name__ == "__main__":
    unittest.main()
