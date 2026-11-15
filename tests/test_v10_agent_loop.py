from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import ANY, patch

import tests.test_v10_execution_store as store_fixture_module
from memory.cognitive_agent_loop import (
    AgentLoopLimits,
    BoundedCognitiveAgentLoop,
    CognitiveAgentLoopError,
    PreparedRuntimeBridgeTurn,
)
from memory.cognitive_authorization import issue_authorization_artifact
from memory.cognitive_cancellation import issue_user_cancellation_artifact
from memory.cognitive_command_policy import CommandTemplateRegistry
from memory.cognitive_execution_capabilities import (
    build_bounded_execution_capability_registry,
)
from memory.cognitive_execution_models import create_tool_proposal
from memory.cognitive_execution_store import CognitiveExecutionStore
from memory.cognitive_local_broker import CognitiveLocalBroker
from memory.cognitive_objective import AuthorityRequest
from memory.cognitive_orchestration import (
    CognitiveContext,
    CognitiveModelRequest,
    CognitiveOrchestrationError,
    ContextBudget,
    ModelBudget,
    ModelBudgetState,
    ProviderBinding,
    STRUCTURED_OUTPUT_CONTRACT_VERSION,
    estimate_tokens,
    parse_structured_model_output,
    reserve_model_call,
    settle_model_call,
)
from memory.cognitive_planning import (
    CapabilityRequirement,
    PlanEdge,
    PlanNode,
    RetryPolicy,
    build_plan_graph,
    pause_plan,
    resume_plan,
)
from memory.cognitive_supervision import ProcessSupervisor
from memory.cognitive_tool_gateway import CognitiveToolGateway
from memory.cognitive_verification import CognitiveIndependentVerifier
from memory.operational_state_models import (
    OperationalStateError,
    canonical_digest,
    canonical_json,
)


T3 = store_fixture_module.T3
T4 = store_fixture_module.T4
T5 = store_fixture_module.T5
T6 = store_fixture_module.T6
T8 = store_fixture_module.T8
T8_20 = store_fixture_module.T8_20
T9 = store_fixture_module.T9


class _FakeBoundedRuntimeBridge:
    """One-shot local model bridge used only by this disposable test."""

    def __init__(self, raw_output: str, registry) -> None:
        self.raw_output = raw_output
        self.registry = registry
        self.send_count = 0

    def prepare_disclosure(
        self,
        selection,
        request,
        reservation,
        *,
        request_id,
        mutation_id,
        budget_state,
    ):
        return SimpleNamespace(
            selection=selection,
            request=request,
            reservation=reservation,
            request_id=request_id,
            mutation_id=mutation_id,
            budget_state=budget_state,
        )

    def preview(
        self,
        disclosure,
        selection,
        request,
        reservation,
        *,
        consent,
        budget_state,
    ):
        return SimpleNamespace(
            disclosure=disclosure,
            selection=selection,
            request=request,
            reservation=reservation,
            consent=consent,
            budget_state=budget_state,
        )

    def execute(
        self,
        preview,
        selection,
        request,
        reservation,
        *,
        consent,
        confirmed,
        authority,
        budget_state,
    ):
        if not confirmed or authority != "fake-runtime-session":
            raise AssertionError("fake runtime authority mismatch")
        self.send_count += 1
        usage = {
            "prompt_tokens": 64,
            "completion_tokens": 32,
            "total_tokens": 96,
        }
        return SimpleNamespace(
            adapter_call_count_this_request=1,
            model_call_count_for_run=1,
            exactly_one_call=True,
            automatic_retry=False,
            replayed=False,
            status="succeeded",
            runtime_model_run_id=f"modelrun:fake:{self.send_count:04d}",
            runtime_receipt_identity=f"rrc_fake_{self.send_count:04d}",
            runtime_artifact_identity=f"artifact:fake:{self.send_count:04d}",
            runtime_usage_digest=canonical_digest(usage),
        )

    @staticmethod
    def read_model_observation(
        preview,
        request,
        reservation,
        *,
        budget_state,
    ):
        return SimpleNamespace(
            status="succeeded",
            usage={
                "prompt_tokens": 64,
                "completion_tokens": 32,
                "total_tokens": 96,
            },
        )

    def reconcile_model_run(
        self,
        preview,
        request,
        reservation,
        *,
        budget_state,
        capability_registry,
    ):
        reason_code = None
        try:
            parsed = parse_structured_model_output(
                self.raw_output,
                request=request,
                capability_registry=capability_registry,
            )
        except CognitiveOrchestrationError as error:
            parsed = None
            reason_code = error.reason_code
        settled = settle_model_call(
            reservation=reservation,
            state=budget_state,
            actual_provider=request.provider_binding.response_identity(),
            actual_input_tokens=64,
            actual_output_tokens=32,
            actual_cost_microunits=0,
            budget=request.model_budget,
        )
        return SimpleNamespace(
            state=(
                "reconciled_succeeded"
                if parsed is not None
                else "rejected_model_output"
            ),
            reason_code=reason_code,
            parsed_output=parsed,
            budget_state=settled,
            grants_authority=False,
            execution_allowed=False,
            system_verified=False,
            user_accepted=False,
        )


class _FakeTurnSource:
    def __init__(
        self,
        *,
        provider_registry,
        capability_registry,
        proposals,
        invalid_output: bool = False,
    ) -> None:
        profile = provider_registry.get("fake", 1)
        self.selection = provider_registry.manual_select(
            provider_id="fake",
            profile_version=1,
            profile_digest=profile.profile_digest,
        )
        self.binding = ProviderBinding(selection=self.selection)
        self.capability_registry = capability_registry
        self.proposals = proposals
        self.invalid_output = invalid_output
        self.bridges = []
        self.prepare_count = 0

    @property
    def total_sends(self) -> int:
        return sum(item.send_count for item in self.bridges)

    def prepare_turn(
        self,
        *,
        run,
        plan,
        node,
        authorization,
        budget_state,
    ) -> PreparedRuntimeBridgeTurn:
        self.prepare_count += 1
        context_payload = {
            "control": {
                "untrusted": True,
                "grants_authority": False,
                "can_trigger_execution": False,
            },
            "plan_node_id": node.node_id,
        }
        context_text = canonical_json(context_payload)
        context = CognitiveContext(
            objective_id=run.objective_id,
            objective_digest=run.objective_digest,
            bindings=(),
            model_context=context_payload,
            budget=ContextBudget(
                maximum_items=1,
                maximum_bytes=4096,
                maximum_estimated_tokens=1024,
            ),
            selected_items=0,
            canonical_bytes=len(context_text.encode("utf-8")),
            estimated_tokens=estimate_tokens(context_text),
        )
        budget = ModelBudget(
            maximum_model_calls=authorization.max_model_calls,
            maximum_input_tokens=min(1024, authorization.max_tokens),
            maximum_output_tokens=min(1024, authorization.max_tokens),
            maximum_total_tokens=min(2048, authorization.max_tokens),
            maximum_cost_microunits=authorization.max_cost_microunits,
        )
        request = CognitiveModelRequest(
            objective={
                "objective_id": run.objective_id,
                "objective_digest": run.objective_digest,
                "interpreted_goal": "Propose one bounded typed tool call.",
                "agency_level": "act",
                "cognitive_stance": "continue",
            },
            context=context,
            provider_binding=self.binding,
            model_budget=budget,
            expected_output_kind="tool_proposal",
            maximum_output_bytes=16_384,
        )
        reservation, next_state = reserve_model_call(
            mode="ai",
            request_digest=request.request_digest,
            provider_binding=request.provider_binding,
            estimated_input_tokens=128,
            requested_output_tokens=256,
            estimated_cost_microunits=1_000,
            provider_context_limit_tokens=2048,
            budget=budget,
            state=budget_state,
        )
        proposal = self.proposals[node.node_id]
        profile = self.capability_registry.lookup(
            proposal["capability_id"],
            version=proposal["capability_version"],
            expected_digest=proposal["capability_digest"],
        )
        envelope = {
            "contract_version": STRUCTURED_OUTPUT_CONTRACT_VERSION,
            "request_digest": request.request_digest,
            "provider_binding": request.provider_binding.response_identity(),
            "output_kind": "tool_proposal",
            "payload": {
                **proposal,
                "risk_class": profile.risk_class,
                "proposal_only": True,
                "authorization_claimed": False,
                "execution_claimed": False,
            },
        }
        if self.invalid_output:
            del envelope["payload"]["expected_observation"]
        bridge = _FakeBoundedRuntimeBridge(
            canonical_json(envelope),
            self.capability_registry,
        )
        self.bridges.append(bridge)
        return PreparedRuntimeBridgeTurn(
            request=request,
            reservation=reservation,
            budget_state_after_reservation=next_state,
            bridge=bridge,
            runtime_authority="fake-runtime-session",
            request_id=f"request:agent-loop:{self.prepare_count:04d}",
            mutation_id=f"mutation:agent-loop:{self.prepare_count:04d}",
            consent_factory=lambda _disclosure: "fake-consent",
        )


class V10BoundedAgentLoopTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixtures = []

    def tearDown(self) -> None:
        for fixture in reversed(self.fixtures):
            fixture.tearDown()

    @staticmethod
    def _authority_with_agent_events(base_cls, instance, **kwargs):
        authority = base_cls.make_authority(instance, **kwargs)
        return replace(
            authority,
            allowed_event_types=tuple(
                dict.fromkeys(
                    (
                        *authority.allowed_event_types,
                        "model_call_reserved",
                        "model_call_settled",
                        "model_call_recovery_required",
                        "verification_record",
                    )
                )
            ),
            authority_digest="",
        )

    def _fixture(self, *, patch_mode: bool = False):
        base_cls = store_fixture_module.CognitiveExecutionStoreTest

        def make_authority(instance, **kwargs):
            return self._authority_with_agent_events(base_cls, instance, **kwargs)

        overrides = {"make_authority": make_authority}

        if patch_mode:
            def persist_ready_plan(instance):
                instance.profile = instance.registry.lookup(
                    "project.file.patch", version=1
                )
                instance.requirement = CapabilityRequirement(
                    capability_id=instance.profile.capability_id,
                    capability_version=instance.profile.version,
                    capability_digest=instance.profile.capability_digest,
                )
                gate = PlanNode(
                    node_id="node:v10-agent-patch-gate",
                    objective_id=instance.objective.objective_id,
                    title="Confirm the disposable patch gate",
                    purpose="Bind the patch to explicit user authority.",
                    node_kind="human_gate",
                    risk="low",
                    reversible=True,
                    authority_requirement="human_gate",
                    success_criteria=("The user gate is complete.",),
                    verification=("Verify the user-authored revision.",),
                    timeout_ms=1_000,
                    state="waiting_authority",
                )
                action = PlanNode(
                    node_id="node:v10-agent-patch",
                    objective_id=instance.objective.objective_id,
                    title="Patch one disposable file",
                    purpose="Replace only README.txt behind exact hash gates.",
                    dependencies=(gate.node_id,),
                    capability_requirements=(instance.requirement,),
                    read_scope=(instance.root_text,),
                    write_scope=(instance.root_text,),
                    risk="medium",
                    reversible=True,
                    authority_requirement="explicit_act",
                    success_criteria=("README.txt has the expected digest.",),
                    verification=("Independently read the current digest and backup.",),
                    rollback=("Restore the owned backup artifact.",),
                    timeout_ms=2_000,
                    retry_policy=RetryPolicy(
                        maximum_attempts=1,
                        equivalent_retry_limit=0,
                        backoff_ms=0,
                        retryable_failures=(),
                    ),
                )
                edge = PlanEdge(
                    edge_id="edge:v10-agent-gate-patch",
                    from_node_id=gate.node_id,
                    to_node_id=action.node_id,
                    edge_kind="resume",
                    condition=None,
                    priority=0,
                )
                instance.now = T3
                graph = build_plan_graph(
                    objective=instance.objective,
                    nodes=(gate, action),
                    edges=(edge,),
                    change_reason="Create the bounded patch plan.",
                    created_at=instance.now,
                    plan_id="plan:v10-execution-store",
                )
                cause = instance.commit_plan(
                    graph,
                    cause=instance.objective_event_id,
                    actor_kind="agent",
                )
                instance.now = T4
                graph = resume_plan(
                    graph,
                    objective=instance.objective,
                    node_ids=(gate.node_id,),
                    reason="The user completed the disposable patch gate.",
                    created_at=instance.now,
                    created_by="user",
                )
                cause = instance.commit_plan(graph, cause=cause, actor_kind="user")
                instance.now = T5
                graph = pause_plan(
                    graph,
                    objective=instance.objective,
                    node_ids=(action.node_id,),
                    reason="Persist a safe patch scheduling checkpoint.",
                    created_at=instance.now,
                    created_by="agent",
                )
                cause = instance.commit_plan(graph, cause=cause, actor_kind="agent")
                instance.now = T6
                graph = resume_plan(
                    graph,
                    objective=instance.objective,
                    node_ids=(action.node_id,),
                    reason="The bounded patch is ready.",
                    created_at=instance.now,
                    created_by="user",
                )
                instance.commit_plan(graph, cause=cause, actor_kind="user")
                return graph

            def issue_and_commit_authorization(instance, *, delegation, nonce):
                authorization = issue_authorization_artifact(
                    objective=instance.objective,
                    plan=instance.plan,
                    delegation=delegation,
                    source_authority=instance.source_authority,
                    read_roots=(instance.root_text,),
                    write_roots=(instance.root_text,),
                    allowed_capabilities=(instance.requirement,),
                    denied_capabilities=(),
                    allowed_commands=(),
                    risk_ceiling="medium",
                    max_steps=2,
                    max_duration_ms=1_800_000,
                    max_model_calls=1,
                    max_tokens=4_096,
                    max_cost_microunits=50_000,
                    commit_permission=False,
                    database_permission="none",
                    valid_from=T8,
                    expires_at=T9,
                    rollback_policy=("Restore the owned backup artifact.",),
                    nonce_material=nonce,
                )
                instance.now = T8_20
                CognitiveExecutionStore(
                    instance.database,
                    trusted_clock=lambda: instance.now,
                ).commit_authorization(
                    authorization=authorization,
                    source_authority=instance.source_authority,
                    lease_owner_id=instance.owner,
                    lease_token=instance.token,
                )
                return authorization

            overrides.update(
                {
                    "persist_ready_plan": persist_ready_plan,
                    "issue_and_commit_authorization": issue_and_commit_authorization,
                }
            )

        fixture_cls = type("_AgentLoopFixture", (base_cls,), overrides)
        fixture = fixture_cls(
            methodName="test_fixture_persists_the_complete_temp_act_chain"
        )

        patches = []
        if patch_mode:
            original_objective_builder = store_fixture_module.build_objective_contract
            original_delegation_contract = store_fixture_module.DelegationContract

            def objective_builder(*args, **kwargs):
                request = kwargs["authority_request"]
                kwargs["risk_tolerance"] = "medium"
                kwargs["authority_request"] = replace(
                    request,
                    write_roots=(fixture.root_text,),
                    capabilities=("project.file.read", "project.file.patch"),
                    risk_ceiling="medium",
                )
                return original_objective_builder(*args, **kwargs)

            def delegation_contract(*args, **kwargs):
                kwargs["risk"] = "medium"
                return original_delegation_contract(*args, **kwargs)

            expanded = build_bounded_execution_capability_registry(
                {
                    "project.file.read": "available",
                    "project.file.patch": "available",
                }
            )
            patches = [
                patch.object(
                    store_fixture_module,
                    "build_bounded_execution_capability_registry",
                    return_value=expanded,
                ),
                patch.object(
                    store_fixture_module,
                    "build_objective_contract",
                    side_effect=objective_builder,
                ),
                patch.object(
                    store_fixture_module,
                    "DelegationContract",
                    side_effect=delegation_contract,
                ),
            ]
        for active in patches:
            active.start()
        try:
            fixture.setUp()
        finally:
            for active in reversed(patches):
                active.stop()
        self.fixtures.append(fixture)
        return fixture

    @staticmethod
    def _proposal(fixture, **typed_arguments):
        return {
            "capability_id": fixture.profile.capability_id,
            "capability_version": fixture.profile.version,
            "capability_digest": fixture.profile.capability_digest,
            "typed_arguments": typed_arguments,
            "reason": "Perform only the bounded persisted Plan node.",
            "expected_observation": "Return a bounded independently verifiable receipt.",
        }

    def _loop(self, fixture, proposals, *, invalid_output: bool = False):
        artifact_root = fixture.root / "agent-artifacts"
        artifact_root.mkdir(exist_ok=True)
        command_registry = CommandTemplateRegistry()
        broker = CognitiveLocalBroker(
            project_root=fixture.root,
            artifact_root=artifact_root,
        )
        gateway = CognitiveToolGateway(
            project_root=str(fixture.root),
            registry=fixture.registry,
            execution_store=fixture.execution_store,
            command_registry=command_registry,
            local_broker=broker,
            process_supervisor=ProcessSupervisor(
                artifact_root=artifact_root,
                trusted_clock=lambda: fixture.now,
            ),
            trusted_clock=lambda: fixture.now,
        )
        verifier = CognitiveIndependentVerifier(
            project_root=fixture.root,
            artifact_root=artifact_root,
            command_registry=command_registry,
        )
        source = _FakeTurnSource(
            provider_registry=fixture.provider_registry,
            capability_registry=fixture.registry,
            proposals=proposals,
            invalid_output=invalid_output,
        )
        loop = BoundedCognitiveAgentLoop(
            execution_store=fixture.execution_store,
            planning_store=fixture.planning_store,
            tool_gateway=gateway,
            independent_verifier=verifier,
            model_turn_source=source,
            source_authority=fixture.source_authority,
            lease_owner_id=fixture.owner,
            lease_token=fixture.token,
            limits=AgentLoopLimits(
                maximum_steps=fixture.authorization.max_steps,
                maximum_duration_ms=fixture.authorization.max_duration_ms,
                maximum_model_calls=fixture.authorization.max_model_calls,
            ),
            trusted_clock=lambda: fixture.now,
        )
        return loop, source, artifact_root

    def test_real_read_runs_once_verifies_and_waits_for_acceptance(self) -> None:
        fixture = self._fixture()
        proposals = {
            fixture.action.node_id: self._proposal(
                fixture,
                relative_path="README.txt",
            )
        }
        loop, source, _artifacts = self._loop(fixture, proposals)
        outcome = loop.run(fixture.run.run_id)

        self.assertEqual("waiting_acceptance", outcome.run.status)
        self.assertEqual(1, outcome.model_sends)
        self.assertEqual(1, source.total_sends)
        self.assertEqual(1, len(outcome.proposal_ids))
        self.assertEqual(1, len(outcome.execution_ids))
        self.assertEqual(1, len(outcome.verification_ids))
        self.assertFalse(outcome.user_accepted)
        persisted_activity = fixture.execution_store.list_supervision_events(
            fixture.run.run_id
        )
        self.assertEqual(1, len(persisted_activity))
        self.assertEqual("checkpoint", persisted_activity[0].event_type)
        self.assertEqual(
            outcome.execution_ids[0],
            persisted_activity[0].tool_execution_id,
        )
        self.assertEqual("bounded fixture\n", (fixture.root / "README.txt").read_text())
        self.assertFalse(fixture.authorization.merge_permission)
        self.assertFalse(fixture.authorization.tag_permission)
        self.assertFalse(fixture.authorization.push_permission)
        self.assertFalse(fixture.authorization.next_version_creation_permission)

        checkpoint_digest = fixture.execution_store.latest_checkpoint(
            fixture.run.run_id
        ).checkpoint_digest
        replay = loop.run(fixture.run.run_id)
        self.assertEqual("waiting_acceptance", replay.run.status)
        self.assertEqual(0, replay.model_sends)
        self.assertEqual(1, source.total_sends)
        self.assertEqual(
            checkpoint_digest,
            fixture.execution_store.latest_checkpoint(
                fixture.run.run_id
            ).checkpoint_digest,
        )
        self.assertEqual(persisted_activity, replay.supervision_events)

    def test_restart_reuses_persisted_unexecuted_proposal_without_model_send(
        self,
    ) -> None:
        fixture = self._fixture()
        proposals = {
            fixture.action.node_id: self._proposal(
                fixture,
                relative_path="README.txt",
            )
        }
        loop, source, _artifacts = self._loop(fixture, proposals)
        running = fixture.execution_store.transition_run(
            run_id=fixture.run.run_id,
            expected_run_digest=fixture.run.run_digest,
            next_status="executing",
            source_authority=fixture.source_authority,
            lease_owner_id=fixture.owner,
            lease_token=fixture.token,
        )
        checkpoint = fixture.execution_store.latest_checkpoint(running.run_id)
        settled, parsed, runtime_reference, runtime_receipt_digest = (
            loop._execute_model_turn(
                run=running,
                plan=fixture.plan,
                node=fixture.action,
                authorization=fixture.authorization,
                budget_state=ModelBudgetState(
                    **dict(checkpoint.model_budget_state)
                ),
            )
        )
        model_proposal = loop._model_proposal(parsed)
        proposal = create_tool_proposal(
            run_id=settled.run_id,
            plan=fixture.plan,
            authorization=fixture.authorization,
            registry=loop.tool_gateway.registry,
            plan_node_id=fixture.action.node_id,
            model_proposal=model_proposal,
            proposed_at=fixture.now,
        )
        fixture.execution_store.record_tool_proposal(
            proposal=proposal,
            source_authority=fixture.source_authority,
            lease_owner_id=fixture.owner,
            lease_token=fixture.token,
            runtime_execution_reference=runtime_reference,
            runtime_receipt_digest=runtime_receipt_digest,
        )
        self.assertEqual(1, source.total_sends)

        restarted = loop.run(fixture.run.run_id)

        self.assertEqual("waiting_acceptance", restarted.run.status)
        self.assertEqual(1, source.total_sends)
        self.assertEqual(0, restarted.model_sends)
        self.assertEqual((proposal.proposal_id,), restarted.proposal_ids)
        self.assertEqual(1, len(restarted.execution_ids))
        self.assertEqual(1, len(restarted.verification_ids))
        self.assertEqual("bounded fixture\n", (fixture.root / "README.txt").read_text())
        self.assertFalse(fixture.authorization.merge_permission)
        self.assertFalse(fixture.authorization.tag_permission)
        self.assertFalse(fixture.authorization.push_permission)
        self.assertFalse(fixture.authorization.next_version_creation_permission)

        persisted_activity = restarted.supervision_events
        checkpoint_digest = fixture.execution_store.latest_checkpoint(
            fixture.run.run_id
        ).checkpoint_digest
        replay = loop.run(fixture.run.run_id)
        self.assertEqual("waiting_acceptance", replay.run.status)
        self.assertEqual(0, replay.model_sends)
        self.assertEqual(1, source.total_sends)
        self.assertEqual(
            checkpoint_digest,
            fixture.execution_store.latest_checkpoint(
                fixture.run.run_id
            ).checkpoint_digest,
        )
        self.assertEqual(persisted_activity, replay.supervision_events)

    def test_restart_rejects_proposal_without_bound_runtime_receipt(self) -> None:
        fixture = self._fixture()
        model_proposal = self._proposal(
            fixture,
            relative_path="README.txt",
        )
        loop, source, _artifacts = self._loop(
            fixture,
            {fixture.action.node_id: model_proposal},
        )
        running = fixture.execution_store.transition_run(
            run_id=fixture.run.run_id,
            expected_run_digest=fixture.run.run_digest,
            next_status="executing",
            source_authority=fixture.source_authority,
            lease_owner_id=fixture.owner,
            lease_token=fixture.token,
        )
        proposal = create_tool_proposal(
            run_id=running.run_id,
            plan=fixture.plan,
            authorization=fixture.authorization,
            registry=loop.tool_gateway.registry,
            plan_node_id=fixture.action.node_id,
            model_proposal=model_proposal,
            proposed_at=fixture.now,
        )
        fixture.execution_store.record_tool_proposal(
            proposal=proposal,
            source_authority=fixture.source_authority,
            lease_owner_id=fixture.owner,
            lease_token=fixture.token,
        )

        outcome = loop.run(fixture.run.run_id)

        self.assertEqual(
            "agent_pending_proposal_runtime_receipt_missing",
            outcome.reason_code,
        )
        self.assertEqual("failed", outcome.run.status)
        self.assertEqual(0, outcome.model_sends)
        self.assertEqual(0, source.total_sends)
        self.assertEqual((), outcome.execution_ids)

    def test_safe_user_cancellation_is_persisted_without_model_or_tool_call(self) -> None:
        fixture = self._fixture()
        loop, source, _artifact_root = self._loop(fixture, [])
        artifact = issue_user_cancellation_artifact(
            source_authority=fixture.source_authority,
            project_id=fixture.authorization.project_id,
            run_id=fixture.run.run_id,
            expected_run_digest=fixture.run.run_digest,
            session_digest="a" * 64,
            csrf_session_proof_digest="b" * 64,
            requested_at=fixture.now,
            expires_at=T9,
            reason="The local user requested cancellation.",
        )

        outcome = loop.request_cancellation(artifact)

        self.assertEqual("cancelled", outcome.run.status)
        self.assertEqual("user_requested_cancellation", outcome.reason_code)
        self.assertEqual(0, outcome.model_sends)
        self.assertEqual(0, source.total_sends)
        self.assertEqual(
            (),
            fixture.execution_store.list_tool_executions(fixture.run.run_id),
        )
        persisted = fixture.execution_store.get_cancellation_request(
            fixture.run.run_id
        )
        self.assertEqual(artifact, persisted)

    def test_unsealed_cancellation_request_is_rejected(self) -> None:
        fixture = self._fixture()
        loop, source, _artifact_root = self._loop(fixture, [])

        with self.assertRaisesRegex(
            CognitiveAgentLoopError,
            "sealed_user_cancellation_artifact_required",
        ):
            loop.request_cancellation(fixture.run.run_id)

        self.assertEqual(0, source.total_sends)

    def test_human_gate_and_decision_wait_without_model_or_tool(self) -> None:
        for node_kind in ("human_gate", "decision"):
            with self.subTest(node_kind=node_kind):
                fixture = self._fixture()
                loop, source, _artifact_root = self._loop(fixture, {})
                node = PlanNode(
                    node_id=f"node:v10-agent-{node_kind}",
                    objective_id=fixture.run.objective_id,
                    title=f"Wait for {node_kind}",
                    purpose="Require explicit authority before any execution.",
                    node_kind=node_kind,
                    authority_requirement="human_gate",
                    success_criteria=("A user resolves this gate.",),
                    verification=("Verify the user-authored decision.",),
                    state="waiting_authority",
                )
                fake_plan = SimpleNamespace(nodes=(node,))
                with patch.object(loop, "_load_plan", return_value=fake_plan), patch.object(
                    loop,
                    "_assert_authorization_boundary",
                    return_value=None,
                ):
                    outcome = loop.run(fixture.run.run_id)

                self.assertEqual("waiting_authority", outcome.run.status)
                self.assertEqual("agent_waiting_authority", outcome.reason_code)
                self.assertEqual(0, outcome.model_sends)
                self.assertEqual(0, source.total_sends)
                self.assertEqual(
                    (),
                    fixture.execution_store.list_tool_executions(
                        fixture.run.run_id
                    ),
                )

    def test_restart_duration_uses_persisted_started_at(self) -> None:
        fixture = self._fixture()
        loop, source, _artifact_root = self._loop(fixture, {})
        running = fixture.execution_store.transition_run(
            run_id=fixture.run.run_id,
            expected_run_digest=fixture.run.run_digest,
            next_status="executing",
            source_authority=fixture.source_authority,
            lease_owner_id=fixture.owner,
            lease_token=fixture.token,
        )
        self.assertEqual("2026-08-01T00:08:30.000000Z", running.started_at)
        fixture.now = "2026-08-01T00:39:00Z"

        outcome = loop.run(running.run_id)

        self.assertEqual("agent_duration_budget_exhausted", outcome.reason_code)
        self.assertEqual("failed", outcome.run.status)
        self.assertEqual(0, source.total_sends)

    def test_recover_with_expired_authorization_never_advances(self) -> None:
        fixture = self._fixture()
        loop, source, _artifact_root = self._loop(fixture, {})
        running = fixture.execution_store.transition_run(
            run_id=fixture.run.run_id,
            expected_run_digest=fixture.run.run_digest,
            next_status="executing",
            source_authority=fixture.source_authority,
            lease_owner_id=fixture.owner,
            lease_token=fixture.token,
        )
        fixture.now = T9

        outcome = loop.recover(running.run_id)

        self.assertEqual(
            "authorization_expired_or_not_yet_valid",
            outcome.reason_code,
        )
        self.assertNotEqual("waiting_acceptance", outcome.run.status)
        self.assertEqual(0, outcome.model_sends)
        self.assertEqual(0, source.total_sends)

    def test_nonzero_retry_backoff_persists_wait_without_immediate_retry(self) -> None:
        fixture = self._fixture()
        loop, source, _artifact_root = self._loop(fixture, {})
        node = replace(
            fixture.action,
            retry_policy=RetryPolicy(
                maximum_attempts=2,
                equivalent_retry_limit=1,
                backoff_ms=5_000,
                retryable_failures=("suspected_stall",),
            ),
        )
        fake_plan = SimpleNamespace(nodes=(node,))
        prior_proposal = SimpleNamespace(proposal_id="toolproposal:prior")
        prior_result = SimpleNamespace(
            finished_at=fixture.now,
            execution_id="execution:prior",
        )
        with patch.object(loop, "_load_plan", return_value=fake_plan), patch.object(
            loop,
            "_assert_authorization_boundary",
            return_value=None,
        ), patch.object(
            loop,
            "_find_pending_equivalent_retry",
            return_value=(prior_proposal, prior_result),
        ), patch.object(
            loop.tool_gateway,
            "execute",
            wraps=loop.tool_gateway.execute,
        ) as execute:
            first = loop.run(fixture.run.run_id)
            checkpoint_digest = fixture.execution_store.latest_checkpoint(
                fixture.run.run_id
            ).checkpoint_digest
            restarted = loop.run(fixture.run.run_id)

        self.assertEqual("waiting_external", first.run.status)
        self.assertEqual("agent_retry_backoff_active", first.reason_code)
        self.assertEqual("waiting_external", restarted.run.status)
        self.assertEqual("agent_retry_backoff_active", restarted.reason_code)
        self.assertEqual(
            checkpoint_digest,
            fixture.execution_store.latest_checkpoint(
                fixture.run.run_id
            ).checkpoint_digest,
        )
        self.assertEqual(0, source.total_sends)
        execute.assert_not_called()

    def test_suspected_stall_retry_requires_proven_no_change_and_is_once_only(self) -> None:
        fixture = self._fixture()
        node = replace(
            fixture.action,
            retry_policy=RetryPolicy(
                maximum_attempts=2,
                equivalent_retry_limit=1,
                backoff_ms=1,
                retryable_failures=("suspected_stall",),
            ),
        )
        verified_no_change = SimpleNamespace(
            status="failed",
            reason_codes=(
                "suspected_stall",
                "tool_execution_no_owned_change",
            ),
        )
        unproven_change = SimpleNamespace(
            status="failed",
            reason_codes=("suspected_stall",),
        )

        self.assertTrue(
            BoundedCognitiveAgentLoop._equivalent_retry_allowed(
                node=node,
                result=verified_no_change,
                verification_result="failed",
                attempt_number=1,
                maximum_attempts=2,
                run_status="executing",
            )
        )
        self.assertFalse(
            BoundedCognitiveAgentLoop._equivalent_retry_allowed(
                node=node,
                result=unproven_change,
                verification_result="failed",
                attempt_number=1,
                maximum_attempts=2,
                run_status="executing",
            )
        )
        self.assertFalse(
            BoundedCognitiveAgentLoop._equivalent_retry_allowed(
                node=node,
                result=verified_no_change,
                verification_result="failed",
                attempt_number=2,
                maximum_attempts=2,
                run_status="executing",
            )
        )

    def test_uncertain_effect_or_recovery_state_never_replans(self) -> None:
        safe_checkpoint = SimpleNamespace(
            safe_to_resume=True,
            unsafe_reason_codes=(),
        )
        uncertain = SimpleNamespace(
            status="recovery_required",
            reason_codes=("tool_execution_outcome_unknown",),
        )
        verified_no_change = SimpleNamespace(
            status="failed",
            reason_codes=("tool_execution_no_owned_change",),
        )
        falsely_claimed_no_change = SimpleNamespace(
            status="failed",
            reason_codes=(
                "tool_execution_no_owned_change",
                "tool_authorization_capability_denied",
            ),
        )
        for run_status, result in (
            ("recovery_required", verified_no_change),
            ("executing", uncertain),
            ("executing", falsely_claimed_no_change),
        ):
            with self.subTest(run_status=run_status, reasons=result.reason_codes):
                self.assertFalse(
                    BoundedCognitiveAgentLoop._bounded_replan_allowed(
                        run=SimpleNamespace(status=run_status),
                        checkpoint=safe_checkpoint,
                        result=result,
                        verification_result="failed",
                    )
                )

    def test_replan_observation_is_exactly_bound_and_once_only(self) -> None:
        fixture = self._fixture()
        loop, _source, _artifact_root = self._loop(fixture, {})
        proposal = SimpleNamespace(
            proposal_id="toolproposal:bounded-replan",
            plan_node_id=fixture.action.node_id,
        )
        evidence = {
            "objective_digest": fixture.run.objective_digest,
            "plan_digest": fixture.plan.plan_digest,
            "node_id": fixture.action.node_id,
            "node_digest": fixture.action.node_digest,
            "authorization_digest": fixture.authorization.authority_digest,
            "prior_proposal_id": proposal.proposal_id,
        }
        event = SimpleNamespace(
            event_type="retry_started",
            detail="bounded_observation_replan_pending",
            plan_node_id=fixture.action.node_id,
            evidence=evidence,
        )
        checkpoint = SimpleNamespace(proposal_ids=(proposal.proposal_id,))
        with patch.object(
            fixture.execution_store,
            "list_supervision_events",
            return_value=(event,),
        ), patch.object(
            fixture.execution_store,
            "get_tool_proposal",
            return_value=proposal,
        ):
            bound = loop._pending_replan_observation(
                run_id=fixture.run.run_id,
                run=fixture.run,
                plan=fixture.plan,
                node=fixture.action,
                authorization=fixture.authorization,
                checkpoint=checkpoint,
            )
            self.assertEqual(evidence, bound)

            tampered_event = SimpleNamespace(
                **{
                    **event.__dict__,
                    "evidence": {**evidence, "plan_digest": "0" * 64},
                }
            )
            with patch.object(
                fixture.execution_store,
                "list_supervision_events",
                return_value=(tampered_event,),
            ), self.assertRaisesRegex(
                CognitiveAgentLoopError,
                "agent_replan_binding_mismatch",
            ):
                loop._pending_replan_observation(
                    run_id=fixture.run.run_id,
                    run=fixture.run,
                    plan=fixture.plan,
                    node=fixture.action,
                    authorization=fixture.authorization,
                    checkpoint=checkpoint,
                )

            consumed = loop._pending_replan_observation(
                run_id=fixture.run.run_id,
                run=fixture.run,
                plan=fixture.plan,
                node=fixture.action,
                authorization=fixture.authorization,
                checkpoint=SimpleNamespace(
                    proposal_ids=(proposal.proposal_id, "toolproposal:replanned")
                ),
            )
            self.assertIsNone(consumed)

    def test_cancellation_latched_after_model_skips_tool_and_verification(self) -> None:
        fixture = self._fixture()
        proposals = {
            fixture.action.node_id: self._proposal(
                fixture,
                relative_path="README.txt",
            )
        }
        loop, source, _artifact_root = self._loop(fixture, proposals)
        original = loop._execute_model_turn

        def execute_then_cancel(**kwargs):
            result = original(**kwargs)
            current = fixture.execution_store.get_run(fixture.run.run_id)
            artifact = issue_user_cancellation_artifact(
                source_authority=fixture.source_authority,
                project_id=fixture.authorization.project_id,
                run_id=current.run_id,
                expected_run_digest=current.run_digest,
                session_digest="c" * 64,
                csrf_session_proof_digest="d" * 64,
                requested_at=fixture.now,
                expires_at=T9,
                reason="Cancel after the model observation.",
            )
            fixture.execution_store.record_cancellation_request(
                artifact=artifact,
                source_authority=fixture.source_authority,
                lease_owner_id=fixture.owner,
                lease_token=fixture.token,
            )
            return result

        with patch.object(loop, "_execute_model_turn", side_effect=execute_then_cancel), patch.object(
            loop.independent_verifier,
            "verify",
            wraps=loop.independent_verifier.verify,
        ) as verify:
            outcome = loop.run(fixture.run.run_id)

        self.assertEqual("user_requested_cancellation", outcome.reason_code)
        self.assertEqual("cancelled", outcome.run.status)
        self.assertEqual(1, source.total_sends)
        self.assertEqual((), outcome.execution_ids)
        verify.assert_not_called()

    def test_cancellation_latched_after_tool_never_verifies_or_accepts(self) -> None:
        fixture = self._fixture()
        proposals = {
            fixture.action.node_id: self._proposal(
                fixture,
                relative_path="README.txt",
            )
        }
        loop, source, _artifact_root = self._loop(fixture, proposals)
        original_execute = loop.tool_gateway.execute

        def execute_then_cancel(**kwargs):
            outcome = original_execute(**kwargs)
            current = fixture.execution_store.get_run(fixture.run.run_id)
            artifact = issue_user_cancellation_artifact(
                source_authority=fixture.source_authority,
                project_id=fixture.authorization.project_id,
                run_id=current.run_id,
                expected_run_digest=current.run_digest,
                session_digest="e" * 64,
                csrf_session_proof_digest="f" * 64,
                requested_at=fixture.now,
                expires_at=T9,
                reason="Cancel after the tool observation.",
            )
            fixture.execution_store.record_cancellation_request(
                artifact=artifact,
                source_authority=fixture.source_authority,
                lease_owner_id=fixture.owner,
                lease_token=fixture.token,
            )
            return outcome

        with patch.object(
            loop.tool_gateway,
            "execute",
            side_effect=execute_then_cancel,
        ), patch.object(
            loop.independent_verifier,
            "verify",
            wraps=loop.independent_verifier.verify,
        ) as verify:
            outcome = loop.run(fixture.run.run_id)

        self.assertEqual("cancellation_recovery_required", outcome.reason_code)
        self.assertEqual("recovery_required", outcome.run.status)
        self.assertEqual(1, source.total_sends)
        self.assertEqual(1, len(outcome.execution_ids))
        self.assertEqual((), outcome.verification_ids)
        verify.assert_not_called()

    def test_recover_cancellation_with_pending_tool_reconciles_without_resend(self) -> None:
        fixture = self._fixture()
        proposals = {
            fixture.action.node_id: self._proposal(
                fixture,
                relative_path="README.txt",
            )
        }
        loop, source, _artifact_root = self._loop(fixture, proposals)

        def reserve_then_cancel(**kwargs):
            fixture.execution_store.reserve_tool_execution(
                proposal=kwargs["proposal"],
                preview=kwargs["preview"],
                source_authority=fixture.source_authority,
                authorization=fixture.authorization,
                lease_owner_id=fixture.owner,
                lease_token=fixture.token,
            )
            current = fixture.execution_store.get_run(fixture.run.run_id)
            artifact = issue_user_cancellation_artifact(
                source_authority=fixture.source_authority,
                project_id=fixture.authorization.project_id,
                run_id=current.run_id,
                expected_run_digest=current.run_digest,
                session_digest="3" * 64,
                csrf_session_proof_digest="4" * 64,
                requested_at=fixture.now,
                expires_at=T9,
                reason="Cancel while the tool reservation is unresolved.",
            )
            fixture.execution_store.record_cancellation_request(
                artifact=artifact,
                source_authority=fixture.source_authority,
                lease_owner_id=fixture.owner,
                lease_token=fixture.token,
            )
            raise RuntimeError("simulated_tool_crash")

        with patch.object(
            loop.tool_gateway,
            "execute",
            side_effect=reserve_then_cancel,
        ), patch.object(
            loop.independent_verifier,
            "verify",
            wraps=loop.independent_verifier.verify,
        ) as verify:
            crashed = loop.run(fixture.run.run_id)
            with patch.object(
                fixture.execution_store,
                "reconcile_reserved_execution",
                wraps=fixture.execution_store.reconcile_reserved_execution,
            ) as reconcile:
                recovered = loop.recover(fixture.run.run_id)

        self.assertEqual("recovery_required", crashed.run.status)
        self.assertEqual("cancellation_recovery_required", recovered.reason_code)
        self.assertEqual("recovery_required", recovered.run.status)
        self.assertEqual(1, source.total_sends)
        verify.assert_not_called()
        reconcile.assert_called_once_with(
            reservation_event_id=ANY,
            decision="mark_recovery_required",
            lease_owner_id=fixture.owner,
            lease_token=fixture.token,
        )

    def test_real_patch_uses_backup_and_independent_hash_verification(self) -> None:
        fixture = self._fixture(patch_mode=True)
        target = fixture.root / "README.txt"
        before = target.read_bytes()
        replacement = "bounded agent patch\n"
        proposals = {
            fixture.action.node_id: self._proposal(
                fixture,
                relative_path="README.txt",
                expected_before_sha256=hashlib.sha256(before).hexdigest(),
                replacement_content=replacement,
                expected_after_sha256=hashlib.sha256(
                    replacement.encode("utf-8")
                ).hexdigest(),
            )
        }
        loop, source, artifacts = self._loop(fixture, proposals)
        outcome = loop.run(fixture.run.run_id)

        self.assertEqual("waiting_acceptance", outcome.run.status)
        self.assertEqual(replacement, target.read_text(encoding="utf-8"))
        self.assertEqual(1, source.total_sends)
        self.assertTrue(list(artifacts.glob("*.json")))
        self.assertTrue(list(artifacts.glob("*.before.bin")))
        self.assertFalse(outcome.user_accepted)
        persisted = fixture.execution_store.list_supervision_events(
            fixture.run.run_id
        )
        self.assertEqual("checkpoint", persisted[-1].event_type)
        self.assertEqual(outcome.execution_ids[-1], persisted[-1].tool_execution_id)

    def test_next_version_write_is_blocked_before_tool_execution(self) -> None:
        fixture = self._fixture(patch_mode=True)
        target = fixture.root / "README.txt"
        before = target.read_bytes()
        replacement = "Create v10-G automatically.\n"
        proposals = {
            fixture.action.node_id: self._proposal(
                fixture,
                relative_path="README.txt",
                expected_before_sha256=hashlib.sha256(before).hexdigest(),
                replacement_content=replacement,
                expected_after_sha256=hashlib.sha256(
                    replacement.encode("utf-8")
                ).hexdigest(),
            )
        }
        loop, source, _artifacts = self._loop(fixture, proposals)
        outcome = loop.run(fixture.run.run_id)

        self.assertEqual("agent_next_version_creation_blocked", outcome.reason_code)
        self.assertEqual("failed", outcome.run.status)
        self.assertEqual(before, target.read_bytes())
        self.assertEqual(1, source.total_sends)
        self.assertEqual(0, len(outcome.execution_ids))
        self.assertEqual("overreach_blocked", outcome.supervision_events[-1].event_type)
        persisted = fixture.execution_store.list_supervision_events(
            fixture.run.run_id
        )
        self.assertEqual(outcome.supervision_events, persisted)

    def test_missing_fail_closed_supervision_receipt_requires_recovery(self) -> None:
        fixture = self._fixture(patch_mode=True)
        target = fixture.root / "README.txt"
        before = target.read_bytes()
        replacement = "Create v10-G automatically.\n"
        proposals = {
            fixture.action.node_id: self._proposal(
                fixture,
                relative_path="README.txt",
                expected_before_sha256=hashlib.sha256(before).hexdigest(),
                replacement_content=replacement,
                expected_after_sha256=hashlib.sha256(
                    replacement.encode("utf-8")
                ).hexdigest(),
            )
        }
        loop, _source, _artifacts = self._loop(fixture, proposals)
        with patch.object(
            fixture.execution_store,
            "record_supervision_event",
            side_effect=OperationalStateError("injected_supervision_failure"),
        ):
            outcome = loop.run(fixture.run.run_id)

        self.assertEqual(
            "agent_supervision_persistence_failed",
            outcome.reason_code,
        )
        self.assertEqual("recovery_required", outcome.run.status)
        checkpoint = fixture.execution_store.latest_checkpoint(
            fixture.run.run_id
        )
        self.assertFalse(checkpoint.safe_to_resume)
        self.assertEqual((), outcome.supervision_events)
        self.assertEqual(before, target.read_bytes())

    def test_unicode_and_spaced_overreach_text_is_normalized_fail_closed(self) -> None:
        for text, reason_code in (
            ("Create v11： automatically.", "agent_next_version_creation_blocked"),
            ("Create v10‑G automatically.", "agent_next_version_creation_blocked"),
            ("Create V 11 automatically.", "agent_next_version_creation_blocked"),
            ("Use POWERＳHELL to continue.", "agent_overreach_blocked"),
            ("Run reset --hard now.", "agent_overreach_blocked"),
        ):
            with self.subTest(text=text), self.assertRaises(
                CognitiveAgentLoopError
            ) as denied:
                BoundedCognitiveAgentLoop._semantic_overreach_guard(
                    {
                        "capability_id": "project.file.read",
                        "typed_arguments": {"relative_path": "README.txt"},
                        "reason": text,
                    }
                )
            self.assertEqual(reason_code, denied.exception.reason_code)

    def test_invalid_structured_output_settles_call_then_fails_closed(self) -> None:
        fixture = self._fixture()
        proposals = {
            fixture.action.node_id: self._proposal(
                fixture,
                relative_path="README.txt",
            )
        }
        loop, source, _artifacts = self._loop(
            fixture,
            proposals,
            invalid_output=True,
        )
        outcome = loop.run(fixture.run.run_id)

        self.assertEqual("invalid_structured_tool_proposal", outcome.reason_code)
        self.assertEqual("failed", outcome.run.status)
        self.assertEqual(1, outcome.run.model_calls_used)
        self.assertEqual(1, source.total_sends)
        self.assertEqual(1, outcome.model_sends)
        self.assertFalse(outcome.proposal_ids)
        self.assertFalse(outcome.execution_ids)
        checkpoint = fixture.execution_store.latest_checkpoint(
            fixture.run.run_id
        )
        self.assertFalse(
            tuple(
                item
                for item in checkpoint.runtime_recovery_bindings
                if item.get("kind") == "model_call_quarantine"
            )
        )
        persisted = fixture.execution_store.list_supervision_events(
            fixture.run.run_id
        )
        self.assertEqual("recovery_started", persisted[-1].event_type)

    def test_reconcile_exception_after_durable_success_is_settled_not_quarantined(self) -> None:
        fixture = self._fixture()
        proposals = {
            fixture.action.node_id: self._proposal(
                fixture,
                relative_path="README.txt",
            )
        }
        loop, source, _artifact_root = self._loop(fixture, proposals)

        with patch.object(
            _FakeBoundedRuntimeBridge,
            "reconcile_model_run",
            side_effect=RuntimeError("structured_parse_failed"),
        ):
            outcome = loop.run(fixture.run.run_id)

        self.assertEqual("structured_parse_failed", outcome.reason_code)
        self.assertEqual("failed", outcome.run.status)
        self.assertEqual(1, outcome.run.model_calls_used)
        self.assertEqual(1, source.total_sends)
        self.assertEqual(1, outcome.model_sends)
        checkpoint = fixture.execution_store.latest_checkpoint(
            fixture.run.run_id
        )
        self.assertNotIn(
            "model_call_reserved_without_settlement",
            checkpoint.unsafe_reason_codes,
        )
        self.assertFalse(
            tuple(
                item
                for item in checkpoint.runtime_recovery_bindings
                if item.get("kind") == "model_call_quarantine"
            )
        )

    def test_durable_terminal_provider_failure_settles_failed_without_retry(self) -> None:
        fixture = self._fixture()
        proposals = {
            fixture.action.node_id: self._proposal(
                fixture,
                relative_path="README.txt",
            )
        }
        loop, source, _artifact_root = self._loop(fixture, proposals)
        usage = {
            "prompt_tokens": 64,
            "completion_tokens": 0,
            "total_tokens": 64,
        }

        def terminal_failure(bridge, *_args, **_kwargs):
            bridge.send_count += 1
            return SimpleNamespace(
                adapter_call_count_this_request=1,
                model_call_count_for_run=1,
                exactly_one_call=True,
                automatic_retry=False,
                replayed=False,
                status="failed",
                runtime_model_run_id="modelrun:fake:failed",
                runtime_receipt_identity="rrc_fake_failed",
                runtime_artifact_identity="artifact:fake:failed",
                runtime_usage_digest=canonical_digest(usage),
            )

        with patch.object(
            _FakeBoundedRuntimeBridge,
            "execute",
            new=terminal_failure,
        ), patch.object(
            _FakeBoundedRuntimeBridge,
            "read_model_observation",
            return_value=SimpleNamespace(status="failed", usage=usage),
        ):
            outcome = loop.run(fixture.run.run_id)

        self.assertEqual("agent_model_call_failed", outcome.reason_code)
        self.assertEqual("failed", outcome.run.status)
        self.assertEqual(1, outcome.run.model_calls_used)
        self.assertEqual(1, source.total_sends)
        self.assertEqual((), outcome.proposal_ids)
        checkpoint = fixture.execution_store.latest_checkpoint(
            fixture.run.run_id
        )
        self.assertFalse(
            tuple(
                item
                for item in checkpoint.runtime_recovery_bindings
                if item.get("kind") == "model_call_quarantine"
            )
        )

    def test_restart_after_persisted_model_reservation_never_resends(self) -> None:
        fixture = self._fixture()
        proposal = self._proposal(fixture, relative_path="README.txt")
        loop, source, _artifacts = self._loop(
            fixture,
            {fixture.action.node_id: proposal},
        )
        run = fixture.execution_store.transition_run(
            run_id=fixture.run.run_id,
            expected_run_digest=fixture.run.run_digest,
            next_status="executing",
            source_authority=fixture.source_authority,
            lease_owner_id=fixture.owner,
            lease_token=fixture.token,
        )
        checkpoint = fixture.execution_store.latest_checkpoint(run.run_id)
        prepared = source.prepare_turn(
            run=run,
            plan=fixture.plan,
            node=fixture.action,
            authorization=fixture.authorization,
            budget_state=ModelBudgetState(**{
                **dict(checkpoint.model_budget_state),
                "request_digests": tuple(checkpoint.model_budget_state["request_digests"]),
                "settled_reservation_ids": tuple(checkpoint.model_budget_state["settled_reservation_ids"]),
                "unknown_cost_reservation_ids": tuple(checkpoint.model_budget_state["unknown_cost_reservation_ids"]),
            }),
        )
        reserved = fixture.execution_store.reserve_model_call(
            run_id=run.run_id,
            expected_run_digest=run.run_digest,
            authorization=fixture.authorization,
            reservation=prepared.reservation,
            provider_id=run.provider_id,
            provider_profile_version=run.provider_profile_version,
            provider_profile_digest=run.provider_profile_digest,
            request_digest=prepared.request.request_digest,
            source_authority=fixture.source_authority,
            lease_owner_id=fixture.owner,
            lease_token=fixture.token,
            budget=prepared.request.model_budget,
        )
        self.assertEqual("reserved", reserved["status"])
        self.assertEqual(0, source.total_sends)

        unresolved_gate = PlanNode(
            node_id="node:v10-agent-unresolved-gate",
            objective_id=fixture.run.objective_id,
            title="Do not mask unresolved work",
            purpose="Recovery must take precedence over a human gate.",
            node_kind="human_gate",
            authority_requirement="human_gate",
            success_criteria=("The user resolves the gate.",),
            verification=("Verify the explicit user decision.",),
            state="waiting_authority",
        )
        with patch.object(
            loop,
            "_load_plan",
            return_value=SimpleNamespace(nodes=(unresolved_gate,)),
        ), patch.object(
            loop,
            "_assert_authorization_boundary",
            return_value=None,
        ):
            restarted = loop.run(run.run_id)
        self.assertEqual("recovery_required", restarted.run.status)
        self.assertEqual(
            "agent_unsettled_operation_recovery_required",
            restarted.reason_code,
        )
        self.assertEqual(0, source.total_sends)
        self.assertEqual(1, source.prepare_count)
        persisted = fixture.execution_store.list_supervision_events(run.run_id)
        self.assertEqual(1, len(persisted))
        self.assertEqual("recovery_started", persisted[0].event_type)

        recovery_run = fixture.execution_store.get_run(run.run_id)
        cancellation = issue_user_cancellation_artifact(
            source_authority=fixture.source_authority,
            project_id=fixture.authorization.project_id,
            run_id=recovery_run.run_id,
            expected_run_digest=recovery_run.run_digest,
            session_digest="1" * 64,
            csrf_session_proof_digest="2" * 64,
            requested_at=fixture.now,
            expires_at=T9,
            reason="Cancel while the model reservation is unresolved.",
        )
        fixture.execution_store.record_cancellation_request(
            artifact=cancellation,
            source_authority=fixture.source_authority,
            lease_owner_id=fixture.owner,
            lease_token=fixture.token,
        )
        quarantined = loop.recover(run.run_id)
        self.assertEqual("recovery_required", quarantined.run.status)
        self.assertEqual("cancellation_recovery_required", quarantined.reason_code)
        self.assertEqual(0, quarantined.model_sends)
        self.assertEqual(0, source.total_sends)
        checkpoint = fixture.execution_store.latest_checkpoint(run.run_id)
        quarantines = tuple(
            item
            for item in checkpoint.runtime_recovery_bindings
            if item.get("kind") == "model_call_quarantine"
        )
        self.assertEqual(1, len(quarantines))

    def test_direct_forbidden_operations_fail_closed_and_cannot_accept(self) -> None:
        for capability_id in (
            "git.merge",
            "git.tag",
            "git.push",
            "shell.execute",
            "network.request",
            "database.write",
        ):
            with self.subTest(capability_id=capability_id):
                with self.assertRaises(CognitiveAgentLoopError) as denied:
                    BoundedCognitiveAgentLoop._semantic_overreach_guard(
                        {
                            "capability_id": capability_id,
                            "typed_arguments": {},
                        }
                    )
                self.assertEqual("agent_overreach_blocked", denied.exception.reason_code)


if __name__ == "__main__":
    unittest.main()
