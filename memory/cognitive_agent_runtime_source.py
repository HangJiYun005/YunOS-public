"""Production wiring for one bounded Agent Loop model turn.

This module grants no tool authority.  It binds an already-persisted Objective,
Plan node and Authorization to one manually selected provider, one minimal
v7/v9 context, one model-budget reservation and the existing v8 Runtime bridge.
"""

from __future__ import annotations

from dataclasses import InitVar, dataclass
from typing import Any, Callable, Mapping

from memory.cognitive_agent_loop import AGENT_LOOP_VERSION, PreparedRuntimeBridgeTurn
from memory.cognitive_authorization import CognitiveAuthorizationArtifact
from memory.cognitive_execution_models import CognitiveRun
from memory.cognitive_external_send import ExternalSendConsent
from memory.cognitive_objective import ObjectiveContract
from memory.cognitive_orchestration import (
    CognitiveContext,
    ContextBudget,
    ModelBudget,
    ModelBudgetState,
    ProviderBinding,
    bind_manual_provider_selection,
    contains_secret_material,
    estimate_tokens,
    prepare_model_request,
    reserve_model_call,
)
from memory.cognitive_planning import PlanGraph, PlanNode
from memory.cognitive_provider import CostMetadata, ProviderRegistry, ProviderSelection
from memory.cognitive_runtime_bridge import (
    MAX_BOUNDED_MODEL_CALLS,
    PRODUCTION_FIXED_OUTPUT_TOKENS,
    PRODUCTION_MODEL_ID,
    PRODUCTION_PROVIDER_ID,
    PRODUCTION_PROVIDER_PROFILE_DIGEST,
    PRODUCTION_PROVIDER_PROFILE_VERSION,
    REQUIRED_CONTEXT_SOURCES,
    MockCognitiveRuntimeBridge,
    ProductionCognitiveRuntimeBridge,
)
from memory.operational_state_models import (
    OperationalStateError,
    canonical_digest,
    canonical_json,
    make_identity,
    normalize_timestamp,
    utc_now,
    validate_identity,
    validate_sha256,
)
from memory.production_runtime_execution import open_production_runtime_execution


AGENT_RUNTIME_SOURCE_VERSION = "v10-d-production-turn-source-2"
_SOURCE_FACTORY_SEAL = object()
_REPLAN_OBSERVATION_FIELDS = frozenset(
    {
        "agent_loop_version",
        "objective_digest",
        "plan_digest",
        "node_id",
        "node_digest",
        "authorization_digest",
        "prior_proposal_id",
        "prior_proposal_digest",
        "prior_execution_id",
        "prior_execution_digest",
        "verification_id",
        "verification_digest",
        "verification_result",
    }
)


class CognitiveAgentRuntimeSourceError(OperationalStateError):
    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


def _fail(reason_code: str) -> None:
    raise CognitiveAgentRuntimeSourceError(reason_code)


def _ceil_token_cost(tokens: int, microunits_per_million: int) -> int:
    return (tokens * microunits_per_million + 999_999) // 1_000_000


def _conservative_cost_reservation(
    *,
    cost_metadata: CostMetadata,
    input_token_ceiling: int,
    output_token_ceiling: int,
    maximum_cost_microunits: int,
) -> int:
    """Reserve a ceiling derived only from registry truth and hard budgets."""

    if not isinstance(cost_metadata, CostMetadata):
        _fail("agent_runtime_cost_metadata_required")
    for name, value in (
        ("input_token_ceiling", input_token_ceiling),
        ("output_token_ceiling", output_token_ceiling),
        ("maximum_cost_microunits", maximum_cost_microunits),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            _fail(f"invalid_agent_runtime_{name}")
    if cost_metadata.pricing_state == "unknown":
        if maximum_cost_microunits == 0:
            _fail("agent_runtime_unknown_cost_budget_required")
        return maximum_cost_microunits
    if cost_metadata.pricing_state == "not_applicable":
        return 0
    if cost_metadata.pricing_state != "known":  # defensive typed-boundary guard
        _fail("invalid_agent_runtime_pricing_state")
    input_rate = cost_metadata.input_microunits_per_million_tokens
    output_rate = cost_metadata.output_microunits_per_million_tokens
    if input_rate is None or output_rate is None:
        _fail("agent_runtime_known_pricing_incomplete")
    computed = _ceil_token_cost(input_token_ceiling, input_rate) + _ceil_token_cost(
        output_token_ceiling,
        output_rate,
    )
    if computed > maximum_cost_microunits:
        _fail("agent_runtime_known_cost_budget_exhausted")
    return computed


@dataclass(frozen=True)
class AgentRuntimeModelLimits:
    maximum_context_items: int = 24
    maximum_context_bytes: int = 65_536
    maximum_input_tokens: int = 8_192
    maximum_total_tokens: int = 9_216
    maximum_cost_microunits: int = 1_000_000
    maximum_output_bytes: int = 65_536

    def __post_init__(self) -> None:
        values = {
            "maximum_context_items": (self.maximum_context_items, 1, 256),
            "maximum_context_bytes": (self.maximum_context_bytes, 256, 4_000_000),
            "maximum_input_tokens": (self.maximum_input_tokens, 1, 10_000_000),
            "maximum_total_tokens": (self.maximum_total_tokens, 1, 20_000_000),
            "maximum_cost_microunits": (
                self.maximum_cost_microunits,
                0,
                10**15,
            ),
            "maximum_output_bytes": (self.maximum_output_bytes, 256, 1_000_000),
        }
        for name, (value, minimum, maximum) in values.items():
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not minimum <= value <= maximum
            ):
                _fail(f"invalid_agent_runtime_{name}")
        if self.maximum_total_tokens <= PRODUCTION_FIXED_OUTPUT_TOKENS:
            _fail("agent_runtime_total_budget_too_small")

    def context_budget(self) -> ContextBudget:
        return ContextBudget(
            maximum_items=self.maximum_context_items,
            maximum_bytes=self.maximum_context_bytes,
            maximum_estimated_tokens=self.maximum_input_tokens,
        )


ContextFactory = Callable[
    [CognitiveRun, PlanGraph, PlanNode, CognitiveAuthorizationArtifact],
    CognitiveContext,
]
ConsentFactory = Callable[
    [Any, ProviderSelection, Any, Any],
    ExternalSendConsent,
]


@dataclass(frozen=True)
class ProductionAgentRuntimeTurnSource:
    """Factory-only source for a single production or explicit mock bridge."""

    objective: ObjectiveContract
    provider_registry: ProviderRegistry
    provider_selection: ProviderSelection
    context_factory: ContextFactory
    consent_factory: ConsentFactory
    bridge: Any
    runtime_authority: Any
    limits: AgentRuntimeModelLimits
    trusted_clock: Callable[[], str]
    source_kind: str
    _factory_seal: InitVar[object | None] = None

    def __post_init__(self, _factory_seal: object | None) -> None:
        if _factory_seal is not _SOURCE_FACTORY_SEAL:
            _fail("agent_runtime_source_factory_required")
        if not isinstance(self.objective, ObjectiveContract):
            _fail("agent_runtime_objective_required")
        if self.objective.agency_level != "act":
            _fail("agent_runtime_act_objective_required")
        if not isinstance(self.provider_registry, ProviderRegistry):
            _fail("agent_runtime_provider_registry_required")
        if not isinstance(self.provider_selection, ProviderSelection):
            _fail("agent_runtime_manual_provider_selection_required")
        self.provider_registry.profile_for_selection(self.provider_selection)
        if (
            self.provider_selection.selection_mode != "manual"
            or self.provider_selection.fallback_used
            or self.provider_selection.grants_authority
        ):
            _fail("agent_runtime_hidden_provider_selection_forbidden")
        if not callable(self.context_factory) or not callable(self.consent_factory):
            _fail("agent_runtime_factory_contract_invalid")
        if not isinstance(self.limits, AgentRuntimeModelLimits):
            _fail("agent_runtime_limits_required")
        if not callable(self.trusted_clock):
            _fail("agent_runtime_trusted_clock_required")
        if self.source_kind == "production":
            if not isinstance(self.bridge, ProductionCognitiveRuntimeBridge):
                _fail("agent_runtime_production_bridge_required")
        elif self.source_kind == "mock":
            if not isinstance(self.bridge, MockCognitiveRuntimeBridge):
                _fail("agent_runtime_mock_bridge_required")
            if self.runtime_authority is not self.bridge.execution_session:
                _fail("agent_runtime_mock_session_mismatch")
        else:
            _fail("invalid_agent_runtime_source_kind")

    @staticmethod
    def _node_from_plan(plan: PlanGraph, node: PlanNode) -> PlanNode:
        persisted = next(
            (candidate for candidate in plan.nodes if candidate.node_id == node.node_id),
            None,
        )
        if persisted is None or persisted != node:
            _fail("agent_runtime_plan_node_binding_mismatch")
        return persisted

    def _assert_exact_boundary(
        self,
        *,
        run: CognitiveRun,
        plan: PlanGraph,
        node: PlanNode,
        authorization: CognitiveAuthorizationArtifact,
        now: str,
    ) -> None:
        self._node_from_plan(plan, node)
        authorization.assert_current(now)
        selection = self.provider_selection
        profile = self.provider_registry.profile_for_selection(selection)
        if selection.health_attestation is not None:
            selection.validate_health_at(now)
        if (
            run.objective_id != self.objective.objective_id
            or run.objective_version != self.objective.version
            or run.objective_digest != self.objective.canonical_digest
            or plan.objective_id != run.objective_id
            or plan.objective_version != run.objective_version
            or plan.objective_digest != run.objective_digest
            or plan.plan_id != run.plan_id
            or plan.version != run.plan_version
            or plan.plan_digest != run.plan_digest
            or node.objective_id != run.objective_id
            or authorization.authorization_id != run.authorization_id
            or authorization.authority_digest != run.authorization_digest
            or authorization.objective_id != run.objective_id
            or authorization.objective_digest != run.objective_digest
            or authorization.plan_id != run.plan_id
            or authorization.plan_version != run.plan_version
            or authorization.plan_digest != run.plan_digest
            or selection.provider_id != run.provider_id
            or selection.profile_version != run.provider_profile_version
            or selection.profile_digest != run.provider_profile_digest
            or profile.model != selection.model
        ):
            _fail("agent_runtime_exact_binding_mismatch")
        # The current v8 production bridge is deliberately DeepSeek-only.  A
        # future provider needs its own reviewed Runtime adapter, not a hidden
        # substitution inside this source.
        if (
            selection.provider_id != PRODUCTION_PROVIDER_ID
            or selection.profile_version != PRODUCTION_PROVIDER_PROFILE_VERSION
            or selection.profile_digest != PRODUCTION_PROVIDER_PROFILE_DIGEST
            or selection.model != PRODUCTION_MODEL_ID
        ):
            _fail("agent_runtime_provider_not_supported_by_v8_bridge")

    def _context(
        self,
        run: CognitiveRun,
        plan: PlanGraph,
        node: PlanNode,
        authorization: CognitiveAuthorizationArtifact,
    ) -> CognitiveContext:
        context = self.context_factory(run, plan, node, authorization)
        if not isinstance(context, CognitiveContext):
            _fail("agent_runtime_context_factory_invalid")
        systems = tuple(sorted(binding.source_system for binding in context.bindings))
        expected = tuple(sorted(REQUIRED_CONTEXT_SOURCES))
        context_limit = self.limits.context_budget()
        if (
            context.objective_id != run.objective_id
            or context.objective_digest != run.objective_digest
            or systems != expected
            or len(set(systems)) != len(systems)
            or context.selected_items > context_limit.maximum_items
            or context.canonical_bytes > context_limit.maximum_bytes
            or context.estimated_tokens > context_limit.maximum_estimated_tokens
        ):
            _fail("agent_runtime_minimal_context_binding_mismatch")
        return context

    @staticmethod
    def _bounded_node_control(
        plan: PlanGraph,
        node: PlanNode,
        authorization: CognitiveAuthorizationArtifact,
    ) -> dict[str, Any]:
        requirements = [item.to_dict() for item in node.capability_requirements]
        if any(
            not authorization.allows_capability(
                item.capability_id,
                item.capability_version,
                item.capability_digest,
            )
            for item in node.capability_requirements
        ):
            _fail("agent_runtime_node_capability_not_authorized")
        control = {
            "contract_version": 1,
            "trust_class": "persisted_bounded_control_data",
            "plan": {
                "plan_id": plan.plan_id,
                "plan_version": plan.version,
                "plan_digest": plan.plan_digest,
            },
            "node": {
                "node_id": node.node_id,
                "node_digest": node.node_digest,
                "title": node.title,
                "purpose": node.purpose,
                "capability_requirements": requirements,
                "read_scope": list(node.read_scope),
                "write_scope": list(node.write_scope),
                "success_criteria": list(node.success_criteria),
                "verification": list(node.verification),
                "risk": node.risk,
                "reversible": node.reversible,
                "authority_requirement": node.authority_requirement,
            },
            "authority_boundary": {
                "authority_binding_digest": authorization.authority_digest,
                "context_grants_authority": False,
                "model_output_grants_authority": False,
                "tool_proposals_execute": False,
                "model_cannot_expand_objective_plan_node_or_scope": True,
                "denied_capabilities": list(authorization.denied_capabilities),
                "commit_permission": authorization.commit_permission,
                "merge_permission": authorization.merge_permission,
                "tag_permission": authorization.tag_permission,
                "push_permission": authorization.push_permission,
                "network_permission": authorization.network_permission,
                "database_permission": authorization.database_permission,
                "next_version_creation_permission": (
                    authorization.next_version_creation_permission
                ),
                "persistent_background_agent_permission": (
                    authorization.persistent_background_agent_permission
                ),
            },
        }
        if contains_secret_material(control):
            _fail("agent_runtime_plan_node_secret_forbidden")
        return control

    @staticmethod
    def _validated_replan_summary(
        *,
        observation: Mapping[str, Any],
        run: CognitiveRun,
        plan: PlanGraph,
        node: PlanNode,
        authorization: CognitiveAuthorizationArtifact,
    ) -> dict[str, Any]:
        if not isinstance(observation, Mapping) or set(observation) != set(
            _REPLAN_OBSERVATION_FIELDS
        ):
            _fail("agent_runtime_replan_observation_schema_mismatch")
        payload = dict(observation)
        expected = {
            "agent_loop_version": AGENT_LOOP_VERSION,
            "objective_digest": run.objective_digest,
            "plan_digest": plan.plan_digest,
            "node_id": node.node_id,
            "node_digest": node.node_digest,
            "authorization_digest": authorization.authority_digest,
            "verification_result": "failed",
        }
        if any(payload.get(key) != value for key, value in expected.items()):
            _fail("agent_runtime_replan_observation_binding_mismatch")
        try:
            for name in (
                "prior_proposal_id",
                "prior_execution_id",
                "verification_id",
            ):
                validate_identity(
                    payload[name],
                    "agent_runtime_replan_observation_identity_invalid",
                )
            if (
                not payload["prior_proposal_id"].startswith("toolproposal:")
                or not payload["prior_execution_id"].startswith(
                    "toolexecution:"
                )
                or not payload["verification_id"].startswith("verification:")
            ):
                _fail("agent_runtime_replan_evidence_kind_mismatch")
            for name in (
                "objective_digest",
                "plan_digest",
                "node_digest",
                "authorization_digest",
                "prior_proposal_digest",
                "prior_execution_digest",
                "verification_digest",
            ):
                validate_sha256(
                    payload[name],
                    "agent_runtime_replan_observation_digest_invalid",
                )
        except OperationalStateError as error:
            _fail(error.reason_code)
        independent_evidence = {
            "prior_proposal_digest": payload["prior_proposal_digest"],
            "prior_execution_digest": payload["prior_execution_digest"],
            "verification_digest": payload["verification_digest"],
        }
        return {
            "observation_digest": canonical_digest(payload),
            "independent_evidence_binding_digest": canonical_digest(
                independent_evidence
            ),
            "reason_code": "independent_verification_failed_no_owned_change",
            "instruction_policy": "data_only_never_authority",
            "grants_authority": False,
        }

    def _sealed_context(
        self,
        *,
        context: CognitiveContext,
        plan: PlanGraph,
        node: PlanNode,
        authorization: CognitiveAuthorizationArtifact,
        replan_summary: Mapping[str, Any] | None,
    ) -> CognitiveContext:
        model_context = dict(context.model_context)
        if (
            "bounded_plan_node" in model_context
            or "bounded_replan_observation" in model_context
        ):
            _fail("agent_runtime_reserved_context_key_collision")
        model_context["bounded_plan_node"] = self._bounded_node_control(
            plan,
            node,
            authorization,
        )
        if replan_summary is not None:
            model_context["bounded_replan_observation"] = dict(replan_summary)
        if contains_secret_material(model_context):
            _fail("agent_runtime_augmented_context_secret_forbidden")
        encoded = canonical_json(model_context)
        canonical_bytes = len(encoded.encode("utf-8"))
        estimated_tokens = estimate_tokens(encoded)
        budget = self.limits.context_budget()
        if (
            context.selected_items > budget.maximum_items
            or canonical_bytes > budget.maximum_bytes
            or estimated_tokens > budget.maximum_estimated_tokens
        ):
            _fail("agent_runtime_augmented_context_budget_exhausted")
        return CognitiveContext(
            objective_id=context.objective_id,
            objective_digest=context.objective_digest,
            bindings=context.bindings,
            model_context=model_context,
            budget=budget,
            selected_items=context.selected_items,
            canonical_bytes=canonical_bytes,
            estimated_tokens=estimated_tokens,
        )

    def _prepare_turn(
        self,
        *,
        run: CognitiveRun,
        plan: PlanGraph,
        node: PlanNode,
        authorization: CognitiveAuthorizationArtifact,
        budget_state: ModelBudgetState,
        replan_summary: Mapping[str, Any] | None,
    ) -> PreparedRuntimeBridgeTurn:
        if not isinstance(run, CognitiveRun) or not isinstance(plan, PlanGraph):
            _fail("agent_runtime_run_plan_required")
        if not isinstance(node, PlanNode):
            _fail("agent_runtime_plan_node_required")
        if not isinstance(authorization, CognitiveAuthorizationArtifact):
            _fail("agent_runtime_authorization_required")
        if not isinstance(budget_state, ModelBudgetState):
            _fail("agent_runtime_budget_state_required")
        now = normalize_timestamp(self.trusted_clock())
        self._assert_exact_boundary(
            run=run,
            plan=plan,
            node=node,
            authorization=authorization,
            now=now,
        )
        context = self._sealed_context(
            context=self._context(run, plan, node, authorization),
            plan=plan,
            node=node,
            authorization=authorization,
            replan_summary=replan_summary,
        )
        profile = self.provider_registry.profile_for_selection(self.provider_selection)
        maximum_model_calls = min(
            authorization.max_model_calls,
            MAX_BOUNDED_MODEL_CALLS,
        )
        if maximum_model_calls < 1:
            _fail("agent_runtime_model_call_budget_exhausted")
        # ``ModelBudget`` is a run-level aggregate while the provider context
        # limit and ``AgentRuntimeModelLimits`` are per-call ceilings.  Split
        # the explicitly authorized run token budget deterministically across
        # the maximum call count, then expose the aggregate ceilings to the
        # shared reservation state.  This preserves the fixed per-call output
        # cap without making call two impossible.
        total_tokens = min(
            authorization.max_tokens // maximum_model_calls,
            profile.context_limit_tokens,
            self.limits.maximum_total_tokens,
        )
        output_tokens = PRODUCTION_FIXED_OUTPUT_TOKENS
        input_tokens = min(
            self.limits.maximum_input_tokens,
            total_tokens - output_tokens,
        )
        if input_tokens <= 0 or context.estimated_tokens > input_tokens:
            _fail("agent_runtime_model_input_budget_exhausted")
        model_budget = ModelBudget(
            maximum_model_calls=maximum_model_calls,
            maximum_input_tokens=input_tokens * maximum_model_calls,
            maximum_output_tokens=output_tokens * maximum_model_calls,
            maximum_total_tokens=(input_tokens + output_tokens)
            * maximum_model_calls,
            maximum_cost_microunits=min(
                authorization.max_cost_microunits,
                self.limits.maximum_cost_microunits,
            ),
        )
        request = prepare_model_request(
            mode="ai",
            objective=self.objective,
            context=context,
            provider_binding=bind_manual_provider_selection(self.provider_selection),
            model_budget=model_budget,
            expected_output_kind="tool_proposal",
            maximum_output_bytes=self.limits.maximum_output_bytes,
        )
        estimated_input = estimate_tokens(canonical_json(request.to_runtime_reference()))
        if estimated_input > input_tokens:
            _fail("agent_runtime_model_input_budget_exhausted")
        cost_budget_for_turn = model_budget.maximum_cost_microunits
        if (
            profile.cost_metadata.pricing_state == "unknown"
            and model_budget.maximum_model_calls > 1
        ):
            # Unknown provider pricing is reserved conservatively per allowed
            # call.  Reserving the entire run ceiling for call one would make
            # every explicitly authorized multi-node Agent run impossible.
            cost_budget_for_turn = (
                model_budget.maximum_cost_microunits
                // model_budget.maximum_model_calls
            )
            if (
                model_budget.maximum_cost_microunits > 0
                and cost_budget_for_turn == 0
            ):
                _fail("agent_runtime_unknown_cost_budget_too_small")
        cost_reservation = _conservative_cost_reservation(
            cost_metadata=profile.cost_metadata,
            input_token_ceiling=input_tokens,
            output_token_ceiling=output_tokens,
            maximum_cost_microunits=cost_budget_for_turn,
        )
        reservation, next_state = reserve_model_call(
            mode="ai",
            request_digest=request.request_digest,
            provider_binding=request.provider_binding,
            estimated_input_tokens=estimated_input,
            requested_output_tokens=output_tokens,
            estimated_cost_microunits=cost_reservation,
            provider_context_limit_tokens=profile.context_limit_tokens,
            budget=model_budget,
            state=budget_state,
        )
        turn_binding = {
            "source_version": AGENT_RUNTIME_SOURCE_VERSION,
            "run_id": run.run_id,
            "run_digest": run.run_digest,
            "objective_digest": run.objective_digest,
            "plan_digest": plan.plan_digest,
            "node_id": node.node_id,
            "node_digest": node.node_digest,
            "authorization_digest": authorization.authority_digest,
            "context_digest": context.context_digest,
            "provider_selection_digest": self.provider_selection.selection_digest,
            "request_digest": request.request_digest,
            "reservation_id": reservation.reservation_id,
            "reservation_sequence": reservation.sequence,
            "turn_kind": "replan" if replan_summary is not None else "initial",
            "replan_observation_digest": (
                replan_summary["observation_digest"]
                if replan_summary is not None
                else None
            ),
        }
        binding_digest = canonical_digest(turn_binding)

        def exact_consent(disclosure: Any) -> ExternalSendConsent:
            consent = self.consent_factory(
                disclosure,
                self.provider_selection,
                request,
                reservation,
            )
            if not isinstance(consent, ExternalSendConsent):
                _fail("agent_runtime_external_send_consent_required")
            try:
                consent.validate_binding(
                    self.provider_selection,
                    request,
                    reservation,
                    **disclosure.consent_binding(),
                    evaluated_at=normalize_timestamp(self.trusted_clock()),
                )
            except Exception:
                _fail("agent_runtime_external_send_consent_binding_mismatch")
            return consent

        return PreparedRuntimeBridgeTurn(
            request=request,
            reservation=reservation,
            budget_state_after_reservation=next_state,
            bridge=self.bridge,
            runtime_authority=self.runtime_authority,
            request_id=make_identity("request", {**turn_binding, "kind": "request"}),
            mutation_id=make_identity("mutation", {**turn_binding, "kind": "mutation"}),
            consent_factory=exact_consent,
        )

    def prepare_turn(
        self,
        *,
        run: CognitiveRun,
        plan: PlanGraph,
        node: PlanNode,
        authorization: CognitiveAuthorizationArtifact,
        budget_state: ModelBudgetState,
    ) -> PreparedRuntimeBridgeTurn:
        return self._prepare_turn(
            run=run,
            plan=plan,
            node=node,
            authorization=authorization,
            budget_state=budget_state,
            replan_summary=None,
        )

    def prepare_replan_turn(
        self,
        *,
        run: CognitiveRun,
        plan: PlanGraph,
        node: PlanNode,
        authorization: CognitiveAuthorizationArtifact,
        budget_state: ModelBudgetState,
        observation: Mapping[str, Any],
    ) -> PreparedRuntimeBridgeTurn:
        """Prepare one separately digested retry request from sealed evidence only.

        The Agent Loop owns the once-only gate.  This source independently
        revalidates every binding and accepts no free-form observation text.
        """

        if not isinstance(run, CognitiveRun) or not isinstance(plan, PlanGraph):
            _fail("agent_runtime_run_plan_required")
        if not isinstance(node, PlanNode):
            _fail("agent_runtime_plan_node_required")
        if not isinstance(authorization, CognitiveAuthorizationArtifact):
            _fail("agent_runtime_authorization_required")
        summary = self._validated_replan_summary(
            observation=observation,
            run=run,
            plan=plan,
            node=node,
            authorization=authorization,
        )
        return self._prepare_turn(
            run=run,
            plan=plan,
            node=node,
            authorization=authorization,
            budget_state=budget_state,
            replan_summary=summary,
        )


def _create_source(
    *,
    objective: ObjectiveContract,
    provider_registry: ProviderRegistry,
    provider_selection: ProviderSelection,
    context_factory: ContextFactory,
    consent_factory: ConsentFactory,
    bridge: Any,
    runtime_authority: Any,
    limits: AgentRuntimeModelLimits,
    trusted_clock: Callable[[], str] | None,
    source_kind: str,
) -> ProductionAgentRuntimeTurnSource:
    return ProductionAgentRuntimeTurnSource(
        objective=objective,
        provider_registry=provider_registry,
        provider_selection=provider_selection,
        context_factory=context_factory,
        consent_factory=consent_factory,
        bridge=bridge,
        runtime_authority=runtime_authority,
        limits=limits,
        trusted_clock=trusted_clock or utc_now,
        source_kind=source_kind,
        _factory_seal=_SOURCE_FACTORY_SEAL,
    )


def create_production_agent_runtime_source(
    *,
    objective: ObjectiveContract,
    provider_registry: ProviderRegistry,
    provider_selection: ProviderSelection,
    context_factory: ContextFactory,
    consent_factory: ConsentFactory,
    limits: AgentRuntimeModelLimits = AgentRuntimeModelLimits(),
    execute: bool,
    allow_external_model_call: bool,
    confirm_deepseek_send: bool,
    trusted_clock: Callable[[], str] | None = None,
) -> ProductionAgentRuntimeTurnSource:
    """Open the existing v8 production session only after all confirmations."""

    authority = open_production_runtime_execution(
        execute=execute,
        allow_external_model_call=allow_external_model_call,
        confirm_deepseek_send=confirm_deepseek_send,
    )
    return _create_source(
        objective=objective,
        provider_registry=provider_registry,
        provider_selection=provider_selection,
        context_factory=context_factory,
        consent_factory=consent_factory,
        bridge=ProductionCognitiveRuntimeBridge(),
        runtime_authority=authority,
        limits=limits,
        trusted_clock=trusted_clock,
        source_kind="production",
    )


def create_mock_agent_runtime_source(
    *,
    objective: ObjectiveContract,
    provider_registry: ProviderRegistry,
    provider_selection: ProviderSelection,
    context_factory: ContextFactory,
    consent_factory: ConsentFactory,
    bridge: MockCognitiveRuntimeBridge,
    limits: AgentRuntimeModelLimits = AgentRuntimeModelLimits(),
    trusted_clock: Callable[[], str] | None = None,
) -> ProductionAgentRuntimeTurnSource:
    """Explicit mock-only injection boundary for disposable Runtime sidecars."""

    if not isinstance(bridge, MockCognitiveRuntimeBridge):
        _fail("agent_runtime_mock_bridge_required")
    return _create_source(
        objective=objective,
        provider_registry=provider_registry,
        provider_selection=provider_selection,
        context_factory=context_factory,
        consent_factory=consent_factory,
        bridge=bridge,
        runtime_authority=bridge.execution_session,
        limits=limits,
        trusted_clock=trusted_clock,
        source_kind="mock",
    )


__all__ = [
    "AGENT_RUNTIME_SOURCE_VERSION",
    "AgentRuntimeModelLimits",
    "CognitiveAgentRuntimeSourceError",
    "ProductionAgentRuntimeTurnSource",
    "create_mock_agent_runtime_source",
    "create_production_agent_runtime_source",
]
