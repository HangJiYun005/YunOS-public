from __future__ import annotations

import json
import inspect
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import memory.cognitive_execution_models as execution_models
from memory.cognitive_authorization import issue_authorization_artifact
from memory.cognitive_cancellation import issue_user_cancellation_artifact
from memory.cognitive_execution_capabilities import (
    build_bounded_execution_capability_registry,
)
from memory.cognitive_execution_models import create_tool_proposal
from memory.cognitive_execution_store import CognitiveExecutionStore
from memory.cognitive_objective import (
    AuthorityRequest,
    ObjectiveSourceAnchor,
    build_objective_contract,
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
from memory.cognitive_planning_store import CognitivePlanningStore
from memory.cognitive_provider import build_suggested_provider_registry
from memory.cognitive_provider_store import CognitiveProviderStore
from memory.cognitive_state_migration import (
    COGNITIVE_SCHEMA_GENERATION,
    apply_cognitive_state_migration,
)
from memory.cognitive_state_store import CognitiveStateStore
from memory.cognitive_supervision import create_supervision_event
from memory.cognitive_verification import (
    CognitiveIndependentVerifier,
    IndependentToolVerification,
    _create_independent_verification,
)
from memory.operational_state_delegation import DelegationContract, DelegationService
from memory.operational_state_models import (
    AuthorityArtifact,
    OperationalStateError,
    SourceAnchor,
    build_state_event,
    canonical_digest,
    canonical_json,
    make_identity,
)
from memory.operational_state_store import (
    OperationalStateStore,
    initialize_operational_database,
)


T0 = "2026-08-01T00:00:00Z"
T1 = "2026-08-01T00:01:00Z"
T2 = "2026-08-01T00:02:00Z"
T3 = "2026-08-01T00:03:00Z"
T4 = "2026-08-01T00:04:00Z"
T5 = "2026-08-01T00:05:00Z"
T6 = "2026-08-01T00:06:00Z"
T7 = "2026-08-01T00:07:00Z"
T8 = "2026-08-01T00:08:00Z"
T8_10 = "2026-08-01T00:08:10Z"
T8_20 = "2026-08-01T00:08:20Z"
T8_30 = "2026-08-01T00:08:30Z"
T9 = "2026-08-01T01:00:00Z"
T_END = "2026-08-01T02:00:00Z"


class CognitiveExecutionStoreTest(unittest.TestCase):
    """Full-store v10-D checks using only a disposable Operational DB."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp.name).resolve(strict=True)
        self.root_text = str(self.root)
        self.database = self.root / "operational.db"
        (self.root / "README.txt").write_text("bounded fixture\n", encoding="utf-8")

        initialize_operational_database(self.database, created_at=T0)
        migration = apply_cognitive_state_migration(self.database, created_at=T1)
        self.assertEqual(COGNITIVE_SCHEMA_GENERATION, migration["schema_generation"])
        self.operational = OperationalStateStore(self.database)
        self.owner = "owner:v10-execution-store"
        self.token = "v10-execution-store-lease"
        self.operational.acquire_writer_lease(
            owner_id=self.owner,
            token=self.token,
            acquired_at=T1,
            expires_at=T_END,
        )
        self.now = T2
        self.anchor = ObjectiveSourceAnchor(
            anchor_id="anchor:v10-execution-store",
            source_kind="user_declaration",
            locator="test:v10-execution-store",
            evidence_digest="a" * 64,
            observed_at=T0,
            privacy_scope="private_metadata",
        )
        self.source_authority = self.make_authority(
            authority_id="authority:v10-execution-store",
            project_id="project:yunos",
            issued_by="user:yunos",
        )
        self.objective = build_objective_contract(
            user_utterance="Run one bounded read-only task in a disposable project.",
            interpreted_goal="Verify the persisted v10-D execution protocol.",
            project_id="project:yunos",
            source_anchors=(self.anchor,),
            success_criteria=(
                "The plan executes only after authorization and reservation.",
            ),
            prohibited_outcomes=(
                "Do not access production data or create a later version.",
            ),
            agency_level="act",
            agency_selected_by="user",
            maximum_authorized_agency_level="act",
            risk_tolerance="low",
            authority_request=AuthorityRequest(
                requested=True,
                read_roots=(self.root_text,),
                write_roots=(),
                capabilities=("project.file.read",),
                risk_ceiling="low",
                database_permission="none",
            ),
        )
        objective_event = build_state_event(
            project_id=self.objective.project_id,
            entity_id="decision:v10-execution-objective",
            entity_type="decision",
            event_type="decision_approved",
            source_kind="declared",
            source_anchor=self.anchor,
            authority_kind="user",
            actor_kind="user",
            actor_id=self.source_authority.issued_by,
            observed_at=T2,
            effective_at=T2,
            recorded_at=T2,
            expires_at=None,
            supersedes_event_id=None,
            caused_by_event_id=None,
            idempotency_key="idempotency:v10-execution-objective",
            conflict_group_id=None,
            payload={
                "state": "approved",
                "objective_id": self.objective.objective_id,
                "objective_version": self.objective.version,
                "objective_digest": self.objective.canonical_digest,
            },
            privacy_scope="private_metadata",
            authority_digest=self.source_authority.authority_digest,
        )
        objective_result = CognitiveStateStore(
            self.database,
            trusted_clock=lambda: self.now,
        ).commit_objective(
            contract=self.objective.to_dict(),
            authority=self.source_authority,
            event=objective_event,
            lease_owner_id=self.owner,
            lease_token=self.token,
            created_at=T2,
        )
        self.objective_event_id = objective_result["event_id"]

        self.registry = build_bounded_execution_capability_registry(
            {"project.file.read": "available"}
        )
        self.planning_store = CognitivePlanningStore(
            self.database,
            trusted_clock=lambda: self.now,
        )
        self.planning_store.install_builtin_capabilities(
            registry=self.registry,
            authority=self.source_authority,
            lease_owner_id=self.owner,
            lease_token=self.token,
        )
        self.provider_registry = build_suggested_provider_registry(allow_fake=True)
        CognitiveProviderStore(
            self.database,
            trusted_clock=lambda: self.now,
        ).install_builtin_providers(
            registry=self.provider_registry,
            authority=self.source_authority,
            lease_owner_id=self.owner,
            lease_token=self.token,
        )
        self.profile = self.registry.lookup("project.file.read", version=1)
        self.requirement = CapabilityRequirement(
            capability_id=self.profile.capability_id,
            capability_version=self.profile.version,
            capability_digest=self.profile.capability_digest,
        )
        self.plan = self.persist_ready_plan()
        self.gate = next(node for node in self.plan.nodes if node.node_kind == "human_gate")
        self.action = next(node for node in self.plan.nodes if node.node_kind == "action")

        self.delegation = self.persist_delegation(
            delegation_id="delegation:v10-execution-store",
            work_item_id="work-item:v10-execution-store",
        )
        self.authorization = self.issue_and_commit_authorization(
            delegation=self.delegation,
            nonce="v10-execution-authorization-nonce-a",
        )
        self.now = T8_30
        self.execution_store = CognitiveExecutionStore(
            self.database,
            trusted_clock=lambda: self.now,
        )
        self.run = self.execution_store.create_run(
            authorization=self.authorization,
            source_authority=self.source_authority,
            provider_id="fake",
            provider_profile_version=1,
            run_nonce="v10-execution-run-nonce-a",
            lease_owner_id=self.owner,
            lease_token=self.token,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def assert_reason(context, expected: str) -> None:
        actual = getattr(context.exception, "reason_code", str(context.exception))
        if actual != expected:
            raise AssertionError(f"expected reason {expected!r}, got {actual!r}")

    def make_authority(
        self,
        *,
        authority_id: str,
        project_id: str,
        issued_by: str,
    ) -> AuthorityArtifact:
        return AuthorityArtifact(
            authority_id=authority_id,
            issued_by=issued_by,
            issued_at=T0,
            expires_at=T_END,
            target_project_id=project_id,
            allowed_event_types=(
                "decision_approved",
                "capability_registry_install",
                "provider_registry_install",
                "plan_created",
                "plan_revised",
                "delegation_proposed",
                "delegation_authorized",
                "delegation_queued",
                "delegation_running",
                "delegation_completed",
                "delegation_accepted",
                "delegation_contract_register",
                "outcome_reported",
                "outcome_projection_update",
                "outcome_verified",
                "outcome_accepted",
                "candidate_create",
                "authorization_artifact_issued",
                "cognitive_run_created",
                "cognitive_run_update",
                "tool_proposal_record",
                "tool_execution_reserved",
                "tool_execution_record",
                "verification_record",
            ),
            read_scope=("*",),
            write_scope=("*",),
            prohibited_operations=(
                "merge",
                "tag",
                "push",
                "start_next_version",
                "create-next-node",
            ),
            commit_permission=True,
            merge_permission=False,
            tag_permission=False,
            push_permission=False,
            next_node_permission=False,
        )

    def plan_event(
        self,
        graph,
        *,
        caused_by_event_id: str,
        actor_kind: str,
    ):
        event_type = "plan_created" if graph.version == 1 else "plan_revised"
        return build_state_event(
            project_id=self.objective.project_id,
            entity_id=make_identity(
                "workitem", {"plan_id": graph.plan_id, "version": graph.version}
            ),
            entity_type="work_item",
            event_type=event_type,
            source_kind="declared",
            source_anchor=self.anchor,
            authority_kind="user",
            actor_kind=actor_kind,
            actor_id=(
                self.source_authority.issued_by
                if actor_kind == "user"
                else "agent:v10-planner"
            ),
            observed_at=self.now,
            effective_at=self.now,
            recorded_at=self.now,
            expires_at=None,
            supersedes_event_id=None,
            caused_by_event_id=caused_by_event_id,
            idempotency_key=f"idempotency:{graph.plan_id}:{graph.version}",
            conflict_group_id=None,
            payload={
                "state": "proposed",
                "plan_id": graph.plan_id,
                "plan_version": graph.version,
                "plan_digest": graph.plan_digest,
                "objective_id": graph.objective_id,
                "objective_version": graph.objective_version,
                "objective_digest": graph.objective_digest,
                "authority_impact": graph.revisions[-1].authority_impact,
            },
            privacy_scope="private_metadata",
            authority_digest=self.source_authority.authority_digest,
        )

    def commit_plan(self, graph, *, cause: str, actor_kind: str):
        result = self.planning_store.commit_plan(
            graph=graph,
            authority=self.source_authority,
            event=self.plan_event(
                graph,
                caused_by_event_id=cause,
                actor_kind=actor_kind,
            ),
            lease_owner_id=self.owner,
            lease_token=self.token,
        )
        return result["event_id"]

    def persist_ready_plan(self):
        gate = PlanNode(
            node_id="node:v10-execution-user-gate",
            objective_id=self.objective.objective_id,
            title="Confirm the bounded execution gate",
            purpose="Bind execution to a persisted user-controlled gate.",
            node_kind="human_gate",
            risk="low",
            reversible=True,
            authority_requirement="human_gate",
            success_criteria=("The user gate is recorded as completed.",),
            verification=("Verify the gate revision actor is the user.",),
            timeout_ms=1_000,
            state="waiting_authority",
        )
        action = PlanNode(
            node_id="node:v10-execution-read",
            objective_id=self.objective.objective_id,
            title="Read the bounded fixture file",
            purpose="Read only the explicitly authorized disposable file.",
            dependencies=(gate.node_id,),
            capability_requirements=(self.requirement,),
            read_scope=(self.root_text,),
            risk="low",
            reversible=True,
            authority_requirement="read",
            success_criteria=("The fixture content is observed.",),
            verification=("Verify the observed content digest.",),
            timeout_ms=1_000,
            retry_policy=RetryPolicy(
                maximum_attempts=2,
                equivalent_retry_limit=1,
                backoff_ms=0,
                retryable_failures=("tool_execution_no_owned_change",),
            ),
        )
        edge = PlanEdge(
            edge_id="edge:v10-execution-gate-read",
            from_node_id=gate.node_id,
            to_node_id=action.node_id,
            edge_kind="resume",
            condition=None,
            priority=0,
        )
        self.now = T3
        graph = build_plan_graph(
            objective=self.objective,
            nodes=(gate, action),
            edges=(edge,),
            change_reason="Create the bounded execution plan.",
            created_at=self.now,
            plan_id="plan:v10-execution-store",
        )
        cause = self.commit_plan(
            graph,
            cause=self.objective_event_id,
            actor_kind="agent",
        )

        self.now = T4
        graph = resume_plan(
            graph,
            objective=self.objective,
            node_ids=(gate.node_id,),
            reason="The user explicitly completed the execution gate.",
            created_at=self.now,
            created_by="user",
        )
        cause = self.commit_plan(graph, cause=cause, actor_kind="user")

        self.now = T5
        graph = pause_plan(
            graph,
            objective=self.objective,
            node_ids=(action.node_id,),
            reason="Persist an explicit safe scheduling checkpoint.",
            created_at=self.now,
            created_by="agent",
        )
        cause = self.commit_plan(graph, cause=cause, actor_kind="agent")

        self.now = T6
        graph = resume_plan(
            graph,
            objective=self.objective,
            node_ids=(action.node_id,),
            reason="The bounded action is ready after the scheduling checkpoint.",
            created_at=self.now,
            created_by="user",
        )
        self.commit_plan(graph, cause=cause, actor_kind="user")
        return graph

    def persist_delegation(
        self,
        *,
        delegation_id: str,
        work_item_id: str,
    ) -> DelegationContract:
        contract = DelegationContract(
            delegation_id=delegation_id,
            project_id=self.objective.project_id,
            work_item_id=work_item_id,
            intent="Execute only the persisted disposable read plan.",
            read_scope=(self.root_text,),
            write_scope=(self.root_text,),
            prohibited_actions=(
                "merge",
                "tag",
                "push",
                "declare_project_complete",
                "declare_project_sealed",
                "resolve_conflict",
                "start_next_version",
                "create_long_term_plan",
                "update_user_profile",
            ),
            tool_scope=(self.profile.capability_id,),
            risk="low",
            valid_from=T7,
            expires_at=T_END,
            acceptance_criteria=("The bounded read is independently verified.",),
            rollback="Discard the disposable test directory.",
            version_scope=("v10-D",),
            commit_permission=False,
            merge_permission=False,
            tag_permission=False,
            push_permission=False,
            next_node_permission=False,
            authority_digest=self.source_authority.authority_digest,
        )
        service = DelegationService(self.operational, source_anchor=self.anchor)
        service.create(
            contract=contract,
            authority=self.source_authority,
            user_actor_id=self.source_authority.issued_by,
            lease_owner_id=self.owner,
            lease_token=self.token,
        )
        for state, at, actor_kind, actor_id in (
            ("authorized", "2026-08-01T00:07:10Z", "user", self.source_authority.issued_by),
            ("queued", "2026-08-01T00:07:20Z", "agent", "agent:v10-loop"),
            ("running", "2026-08-01T00:07:30Z", "agent", "agent:v10-loop"),
        ):
            service.transition(
                delegation_id=contract.delegation_id,
                next_state=state,
                authority=self.source_authority,
                actor_kind=actor_kind,
                actor_id=actor_id,
                at=at,
                lease_owner_id=self.owner,
                lease_token=self.token,
                reason_code=f"test_{state}",
            )
        return contract

    def issue_and_commit_authorization(self, *, delegation, nonce: str):
        authorization = issue_authorization_artifact(
            objective=self.objective,
            plan=self.plan,
            delegation=delegation,
            source_authority=self.source_authority,
            read_roots=(self.root_text,),
            write_roots=(),
            allowed_capabilities=(self.requirement,),
            denied_capabilities=(),
            allowed_commands=(),
            risk_ceiling="low",
            max_steps=2,
            max_duration_ms=1_800_000,
            max_model_calls=1,
            max_tokens=4_096,
            max_cost_microunits=50_000,
            commit_permission=False,
            database_permission="none",
            valid_from=T8,
            expires_at=T9,
            rollback_policy=(),
            nonce_material=nonce,
        )
        self.now = T8_20
        CognitiveExecutionStore(
            self.database,
            trusted_clock=lambda: self.now,
        ).commit_authorization(
            authorization=authorization,
            source_authority=self.source_authority,
            lease_owner_id=self.owner,
            lease_token=self.token,
        )
        return authorization

    def transition(self, run, next_status: str, **kwargs):
        return self.execution_store.transition_run(
            run_id=run.run_id,
            expected_run_digest=run.run_digest,
            next_status=next_status,
            source_authority=self.source_authority,
            lease_owner_id=self.owner,
            lease_token=self.token,
            **kwargs,
        )

    def cancellation_for(self, run, *, reason="User requested cancellation."):
        return issue_user_cancellation_artifact(
            source_authority=self.source_authority,
            project_id=self.authorization.project_id,
            run_id=run.run_id,
            expected_run_digest=run.run_digest,
            session_digest="c" * 64,
            csrf_session_proof_digest="d" * 64,
            requested_at=self.now,
            expires_at=T9,
            reason=reason,
        )

    def proposal_for(self, run=None, authorization=None):
        selected_run = run or self.run
        selected_authorization = authorization or self.authorization
        return create_tool_proposal(
            run_id=selected_run.run_id,
            plan=self.plan,
            authorization=selected_authorization,
            registry=self.registry,
            plan_node_id=self.action.node_id,
            model_proposal={
                "capability_id": self.profile.capability_id,
                "capability_version": self.profile.version,
                "capability_digest": self.profile.capability_digest,
                "typed_arguments": {"relative_path": "README.txt"},
                "reason": "Read only the authorized disposable fixture.",
                "expected_observation": "Return content and its SHA-256 digest.",
            },
            proposed_at=self.now,
        )

    def persist_proposal(self, proposal):
        return self.execution_store.record_tool_proposal(
            proposal=proposal,
            source_authority=self.source_authority,
            lease_owner_id=self.owner,
            lease_token=self.token,
        )

    def preview_for(self, proposal, authorization, attempt_number: int):
        return execution_models._create_tool_execution_preview(
            preview_id=make_identity(
                "preview",
                {
                    "proposal_digest": proposal.proposal_digest,
                    "attempt_number": attempt_number,
                },
            ),
            proposal_digest=proposal.proposal_digest,
            authorization_digest=authorization.authority_digest,
            plan_digest=proposal.plan_digest,
            node_digest=self.action.node_digest,
            capability_digest=proposal.capability_digest,
            attempt_number=attempt_number,
            invocation_kind="native_call",
            resolved_argv=(),
            working_directory=self.root_text,
            resolved_bindings=(),
            template_id=None,
            template_digest=None,
            effective_timeout_ms=1_000,
            effective_output_bytes=self.profile.max_output_bytes,
            prepared_at=self.now,
            expires_at=T9,
        )

    def reserve(self, proposal, authorization=None, attempt_number: int = 1):
        selected_authorization = authorization or self.authorization
        return self.execution_store.reserve_tool_execution(
            proposal=proposal,
            preview=self.preview_for(
                proposal,
                selected_authorization,
                attempt_number,
            ),
            source_authority=self.source_authority,
            authorization=selected_authorization,
            lease_owner_id=self.owner,
            lease_token=self.token,
        )

    def execution_result(
        self,
        proposal,
        reservation,
        *,
        suffix: str,
        status: str,
        verification_status: str,
        reason_codes: tuple[str, ...],
    ):
        return execution_models._create_tool_execution_result(
            execution_id=f"execution:v10-store-{suffix}",
            proposal_id=proposal.proposal_id,
            proposal_digest=proposal.proposal_digest,
            run_id=proposal.run_id,
            idempotency_key=reservation["execution_key"],
            reservation_event_id=reservation["reservation_event_id"],
            reservation_receipt_digest=reservation["reservation_receipt_digest"],
            preview_id=reservation["preview_id"],
            preview_digest=reservation["preview_digest"],
            attempt_number=reservation["attempt_number"],
            invocation_kind="native_call",
            executor_kind="native",
            resolved_argv=(),
            working_directory=self.root_text,
            policy_decision="allow",
            reason_codes=reason_codes,
            started_at=self.now,
            finished_at=self.now,
            status=status,
            exit_code=None,
            output={"operation_executed": True, "owned_change": False},
            stdout_digest=None,
            stderr_digest=None,
            artifact_references=(),
            runtime_execution_reference=None,
            runtime_receipt_digest=None,
            verification_status=verification_status,
        )

    def record_independent(
        self,
        run,
        execution,
        *,
        result: str,
        evidence_digest: str,
    ):
        proposal = self.execution_store.get_tool_proposal(execution.proposal_id)
        reason_code = f"independent_fixture_{result}"
        evidence = {
            "verification_protocol_version": 1,
            "tool_protocol_version": execution_models.TOOL_PROTOCOL_VERSION,
            "result": result,
            "reason_code": reason_code,
            "proposal_digest": proposal.proposal_digest,
            "preview_digest": execution.preview_digest,
            "execution_digest": execution.execution_digest,
            "observation": {
                "method": "independent_disposable_fixture_query",
                "collision_marker": evidence_digest,
            },
        }
        sealed_evidence_digest = canonical_digest(evidence)
        verified_at = execution.finished_at
        verification = _create_independent_verification(
            verification_id=make_identity(
                "verification",
                {
                    "run_id": proposal.run_id,
                    "proposal_digest": proposal.proposal_digest,
                    "preview_digest": execution.preview_digest,
                    "execution_digest": execution.execution_digest,
                    "evidence_digest": sealed_evidence_digest,
                    "verified_at": verified_at,
                },
            ),
            run_id=proposal.run_id,
            objective_id=proposal.objective_id,
            plan_node_id=proposal.plan_node_id,
            tool_execution_id=execution.execution_id,
            verification_kind=CognitiveIndependentVerifier._verification_kind(
                proposal.capability_id
            ),
            verifier_kind="independent_tool",
            criteria=(
                *CognitiveIndependentVerifier._criteria(proposal.capability_id),
                f"observation:{reason_code}",
            ),
            result=result,
            evidence_references=("test:disposable-operational-db",),
            evidence=evidence,
            evidence_digest=sealed_evidence_digest,
            verified_at=verified_at,
            acceptance_state="not_requested",
            acceptance_event_id=None,
            proposal_digest=proposal.proposal_digest,
            execution_digest=execution.execution_digest,
            preview_digest=execution.preview_digest,
        )
        return self.execution_store.record_independent_tool_verification(
            verification=verification,
            expected_run_digest=run.run_digest,
            source_authority=self.source_authority,
            lease_owner_id=self.owner,
            lease_token=self.token,
        )

    def second_run(self):
        delegation = self.persist_delegation(
            delegation_id="delegation:v10-execution-store-b",
            work_item_id="work-item:v10-execution-store-b",
        )
        authorization = self.issue_and_commit_authorization(
            delegation=delegation,
            nonce="v10-execution-authorization-nonce-b",
        )
        self.now = T8_30
        run = self.execution_store.create_run(
            authorization=authorization,
            source_authority=self.source_authority,
            provider_id="fake",
            provider_profile_version=1,
            run_nonce="v10-execution-run-nonce-b",
            lease_owner_id=self.owner,
            lease_token=self.token,
        )
        return authorization, run

    def test_fixture_persists_the_complete_temp_act_chain(self) -> None:
        with closing(sqlite3.connect(self.database)) as connection:
            counts = {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "objective_contracts",
                    "plan_graphs",
                    "capability_registry",
                    "provider_registry",
                    "delegation_contracts",
                    "authorization_artifacts",
                    "cognitive_runs",
                )
            }
        self.assertEqual(1, counts["objective_contracts"])
        self.assertEqual(4, counts["plan_graphs"])
        self.assertEqual(19, counts["capability_registry"])
        self.assertEqual(5, counts["provider_registry"])
        self.assertEqual(1, counts["delegation_contracts"])
        self.assertEqual(1, counts["authorization_artifacts"])
        self.assertEqual(1, counts["cognitive_runs"])
        self.assertEqual("act", self.objective.agency_level)
        self.assertEqual("ready", self.action.state)
        self.assertEqual("completed", self.gate.state)

    def test_cancellation_store_replay_is_idempotent_and_terminal_safe(self) -> None:
        artifact = self.cancellation_for(self.run)
        first = self.execution_store.record_cancellation_request(
            artifact=artifact,
            source_authority=self.source_authority,
            lease_owner_id=self.owner,
            lease_token=self.token,
        )
        self.assertEqual("cancelled", first["run"].status)
        self.assertFalse(first["idempotent_replay"])

        replay = self.execution_store.record_cancellation_request(
            artifact=artifact,
            source_authority=self.source_authority,
            lease_owner_id=self.owner,
            lease_token=self.token,
        )
        self.assertEqual("cancelled", replay["run"].status)
        self.assertTrue(replay["idempotent_replay"])
        persisted = self.execution_store.get_cancellation_request(self.run.run_id)
        self.assertIsNotNone(persisted)
        self.assertEqual(artifact.canonical_digest, persisted.canonical_digest)

        conflicting = self.cancellation_for(
            replay["run"],
            reason="A distinct second cancellation must not be appended.",
        )
        with self.assertRaises(OperationalStateError) as collision:
            self.execution_store.record_cancellation_request(
                artifact=conflicting,
                source_authority=self.source_authority,
                lease_owner_id=self.owner,
                lease_token=self.token,
            )
        self.assert_reason(
            collision,
            "cognitive_cancellation_replay_collision",
        )

        _authorization, other = self.second_run()
        failed = self.transition(
            other,
            "failed",
            failure_code="test_terminal_failure",
        )
        terminal_artifact = self.cancellation_for(failed)
        with self.assertRaises(OperationalStateError) as terminal:
            self.execution_store.record_cancellation_request(
                artifact=terminal_artifact,
                source_authority=self.source_authority,
                lease_owner_id=self.owner,
                lease_token=self.token,
            )
        self.assert_reason(terminal, "terminal_run_cannot_be_cancelled")
        self.assertEqual(
            "failed",
            self.execution_store.get_run(failed.run_id).status,
        )

    def test_authorization_binds_and_revalidates_persisted_delegation_digest(self) -> None:
        stored = self.operational.get_delegation_contract(self.delegation.delegation_id)
        loaded = self.execution_store.get_authorization(
            self.authorization.authorization_id
        )
        self.assertEqual(self.delegation.contract_digest, stored["contract_digest"])
        self.assertEqual(self.delegation.contract_digest, loaded.delegation_digest)
        self.assertEqual(self.source_authority.authority_digest, loaded.source_authority_digest)

        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("DROP TRIGGER delegation_contracts_no_update")
            connection.execute(
                "UPDATE delegation_contracts SET contract_digest=? WHERE delegation_id=?",
                ("0" * 64, self.delegation.delegation_id),
            )
            connection.commit()
            with self.assertRaises(OperationalStateError) as tampered:
                self.execution_store._validate_persisted_bindings(
                    connection,
                    self.authorization,
                )
            self.assert_reason(tampered, "authorization_delegation_record_mismatch")
        with self.assertRaises(OperationalStateError) as schema_guard:
            self.execution_store.get_authorization(self.authorization.authorization_id)
        self.assert_reason(schema_guard, "operational_schema_drift")

    def test_authorization_is_a_single_run_budget(self) -> None:
        replay = self.execution_store.create_run(
            authorization=self.authorization,
            source_authority=self.source_authority,
            provider_id="fake",
            provider_profile_version=1,
            run_nonce="v10-execution-run-nonce-a",
            lease_owner_id=self.owner,
            lease_token=self.token,
        )
        self.assertEqual(self.run, replay)
        with self.assertRaises(OperationalStateError) as second:
            self.execution_store.create_run(
                authorization=self.authorization,
                source_authority=self.source_authority,
                provider_id="fake",
                provider_profile_version=1,
                run_nonce="v10-execution-run-nonce-other",
                lease_owner_id=self.owner,
                lease_token=self.token,
            )
        self.assert_reason(second, "cognitive_authorization_already_bound_to_run")
        self.assertEqual(2, self.authorization.max_steps)
        self.assertEqual(1, self.authorization.max_model_calls)
        self.assertEqual(4_096, self.authorization.max_tokens)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(
                1,
                connection.execute(
                    "SELECT COUNT(*) FROM cognitive_runs WHERE authorization_id=?",
                    (self.authorization.authorization_id,),
                ).fetchone()[0],
            )

    def test_transition_checkpoint_cas_and_expired_resume_fail_closed(self) -> None:
        executing = self.transition(self.run, "executing")
        checkpoint = self.execution_store.latest_checkpoint(self.run.run_id)
        self.assertEqual(2, checkpoint.checkpoint_sequence)
        self.assertEqual("executing", checkpoint.run_status)
        self.assertTrue(checkpoint.safe_to_resume)
        waiting = self.transition(executing, "waiting_external")
        with self.assertRaises(OperationalStateError) as stale:
            self.execution_store.transition_run(
                run_id=self.run.run_id,
                expected_run_digest=executing.run_digest,
                next_status="executing",
                source_authority=self.source_authority,
                lease_owner_id=self.owner,
                lease_token=self.token,
            )
        self.assert_reason(stale, "cognitive_run_projection_conflict")

        self.now = T9
        with self.assertRaises(OperationalStateError) as expired:
            self.transition(waiting, "executing")
        self.assert_reason(expired, "authorization_expired_or_not_yet_valid")
        current = self.execution_store.get_run(self.run.run_id)
        self.assertEqual(waiting.run_digest, current.run_digest)
        self.assertEqual(3, current.checkpoint_sequence)

    def test_proposal_must_be_persisted_before_reservation(self) -> None:
        self.run = self.transition(self.run, "executing")
        proposal = self.proposal_for()
        with self.assertRaises(OperationalStateError) as missing:
            self.reserve(proposal)
        self.assert_reason(missing, "tool_reservation_proposal_not_persisted")
        recorded = self.persist_proposal(proposal)
        loaded = self.execution_store.get_tool_proposal(proposal.proposal_id)
        self.assertFalse(recorded["idempotent_replay"])
        self.assertEqual(proposal.proposal_digest, loaded.proposal_digest)
        self.assertEqual(proposal.run_id, loaded.run_id)

    def test_completed_human_gate_satisfies_dependency_before_reservation(self) -> None:
        initial = self.execution_store.latest_checkpoint(self.run.run_id)
        self.assertIn(self.gate.node_id, initial.completed_node_ids)
        self.assertNotIn(self.action.node_id, initial.completed_node_ids)
        self.run = self.transition(self.run, "executing")
        proposal = self.proposal_for()
        self.persist_proposal(proposal)
        reservation = self.reserve(proposal)
        self.assertEqual("reserved", reservation["status"])
        checkpoint = self.execution_store.latest_checkpoint(self.run.run_id)
        self.assertFalse(checkpoint.safe_to_resume)
        self.assertIn(self.action.node_id, checkpoint.current_node_ids)
        self.assertIn("tool_execution_reserved_without_terminal", checkpoint.unsafe_reason_codes)

    def test_reservation_is_durable_before_effect_and_replay_requires_recovery(self) -> None:
        self.run = self.transition(self.run, "executing")
        proposal = self.proposal_for()
        self.persist_proposal(proposal)
        reservation = self.reserve(proposal)
        with closing(sqlite3.connect(self.database)) as connection:
            event = connection.execute(
                "SELECT event_type,canonical_payload FROM state_events WHERE event_id=?",
                (reservation["reservation_event_id"],),
            ).fetchone()
            terminal_count = connection.execute(
                "SELECT COUNT(*) FROM tool_executions WHERE idempotency_key=?",
                (reservation["execution_key"],),
            ).fetchone()[0]
        self.assertEqual("tool_execution_reserved", event[0])
        self.assertFalse(json.loads(event[1])["operation_executed"])
        self.assertEqual(0, terminal_count)
        replay = self.reserve(proposal)
        self.assertEqual("recovery_required", replay["status"])
        self.assertEqual("tool_execution_outcome_unknown", replay["reason_code"])

    def test_attempt_two_requires_verified_no_owned_change_failure(self) -> None:
        self.run = self.transition(self.run, "executing")
        proposal = self.proposal_for()
        self.persist_proposal(proposal)
        first_reservation = self.reserve(proposal)
        with self.assertRaises(OperationalStateError) as unknown:
            self.reserve(proposal, attempt_number=2)
        self.assert_reason(unknown, "tool_execution_outcome_unknown")

        failed = self.execution_result(
            proposal,
            first_reservation,
            suffix="attempt-one-failed",
            status="failed",
            verification_status="failed",
            reason_codes=("tool_execution_no_owned_change",),
        )
        self.execution_store.record_tool_execution(
            result=failed,
            source_authority=self.source_authority,
            lease_owner_id=self.owner,
            lease_token=self.token,
            current_node_ids=(self.action.node_id,),
        )
        verifying = self.execution_store.get_run(self.run.run_id)
        self.assertEqual("verifying", verifying.status)
        with self.assertRaises(OperationalStateError) as verification_gate:
            self.reserve(proposal, attempt_number=2)
        self.assert_reason(verification_gate, "tool_reservation_run_not_executing")
        verified = self.record_independent(
            verifying,
            failed,
            result="failed",
            evidence_digest="e" * 64,
        )
        self.assertEqual("executing", verified["run"].status)
        self.assertTrue(verified["run"].last_safe_checkpoint)
        self.run = verified["run"]
        second_reservation = self.reserve(proposal, attempt_number=2)
        self.assertEqual("reserved", second_reservation["status"])
        self.assertNotEqual(
            first_reservation["execution_key"], second_reservation["execution_key"]
        )

    def test_terminal_replay_is_bound_to_run_a_not_run_b(self) -> None:
        self.run = self.transition(self.run, "executing")
        proposal_a = self.proposal_for()
        self.persist_proposal(proposal_a)
        reservation_a = self.reserve(proposal_a)
        succeeded = self.execution_result(
            proposal_a,
            reservation_a,
            suffix="run-a-success",
            status="succeeded",
            verification_status="passed",
            reason_codes=("policy_allow",),
        )
        self.execution_store.record_tool_execution(
            result=succeeded,
            source_authority=self.source_authority,
            lease_owner_id=self.owner,
            lease_token=self.token,
            completed_node_ids=(self.action.node_id,),
        )
        replay_a = self.reserve(proposal_a)
        self.assertEqual("terminal_replay", replay_a["status"])
        self.assertEqual(succeeded.execution_id, replay_a["execution"].execution_id)

        authorization_b, run_b = self.second_run()
        run_b = self.transition(run_b, "executing")
        proposal_b = self.proposal_for(run_b, authorization_b)
        self.persist_proposal(proposal_b)
        reservation_b = self.reserve(proposal_b, authorization_b)
        self.assertEqual("reserved", reservation_b["status"])
        self.assertNotEqual(reservation_a["execution_key"], reservation_b["execution_key"])
        self.assertIsNone(
            self.execution_store.get_execution_by_idempotency(
                reservation_b["execution_key"]
            )
        )

    def test_independent_verification_entrypoint_requires_factory_sealed_artifact(
        self,
    ) -> None:
        record = self.execution_store.record_independent_tool_verification
        parameters = set(inspect.signature(record).parameters)
        self.assertIn("verification", parameters)
        self.assertTrue(
            {
                "run_id",
                "execution_id",
                "result",
                "evidence_digest",
                "verification_kind",
                "criteria",
                "evidence_references",
                "details",
                "verifier_kind",
            }.isdisjoint(parameters)
        )
        with self.assertRaises(OperationalStateError) as mapping_denied:
            record(
                verification={"result": "passed"},
                expected_run_digest=self.run.run_digest,
                source_authority=self.source_authority,
                lease_owner_id=self.owner,
                lease_token=self.token,
            )
        self.assert_reason(
            mapping_denied,
            "sealed_independent_tool_verification_required",
        )

        forged = object.__new__(IndependentToolVerification)
        with self.assertRaises(OperationalStateError) as unsealed_denied:
            record(
                verification=forged,
                expected_run_digest=self.run.run_digest,
                source_authority=self.source_authority,
                lease_owner_id=self.owner,
                lease_token=self.token,
            )
        self.assert_reason(
            unsealed_denied,
            "independent_tool_verification_factory_required",
        )

    def test_independent_verification_replay_ignores_later_clock_but_not_semantics(
        self,
    ) -> None:
        self.run = self.transition(self.run, "executing")
        proposal = self.proposal_for()
        self.persist_proposal(proposal)
        reservation = self.reserve(proposal)
        succeeded = self.execution_result(
            proposal,
            reservation,
            suffix="verification-replay",
            status="succeeded",
            verification_status="passed",
            reason_codes=("policy_allow",),
        )
        self.execution_store.record_tool_execution(
            result=succeeded,
            source_authority=self.source_authority,
            lease_owner_id=self.owner,
            lease_token=self.token,
        )
        verifying = self.execution_store.get_run(self.run.run_id)
        recorded = self.record_independent(
            verifying,
            succeeded,
            result="passed",
            evidence_digest="7" * 64,
        )

        self.now = T8_30
        replay = self.record_independent(
            recorded["run"],
            succeeded,
            result="passed",
            evidence_digest="7" * 64,
        )
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(recorded["verification_digest"], replay["verification_digest"])
        self.assertEqual("executing", replay["run"].status)
        with self.assertRaises(OperationalStateError) as collision:
            self.record_independent(
                recorded["run"],
                succeeded,
                result="passed",
                evidence_digest="8" * 64,
            )
        self.assert_reason(collision, "tool_verification_replay_collision")

    def test_supervision_event_is_persisted_ordered_and_idempotent(self) -> None:
        self.run = self.transition(self.run, "executing")
        checkpoint_before = self.execution_store.latest_checkpoint(self.run.run_id)
        event = create_supervision_event(
            run_id=self.run.run_id,
            sequence=1,
            event_type="heartbeat",
            severity="info",
            observed_at=self.now,
            detail="Bounded loop reached a durable checkpoint.",
            evidence={"checkpoint_digest": checkpoint_before.checkpoint_digest},
            action_taken=("checkpoint_persisted",),
            plan_node_id=self.action.node_id,
        )
        recorded = self.execution_store.record_supervision_event(
            event=event,
            expected_run_digest=self.run.run_digest,
            source_authority=self.source_authority,
            lease_owner_id=self.owner,
            lease_token=self.token,
        )
        self.assertFalse(recorded["idempotent_replay"])
        self.assertEqual(
            checkpoint_before.checkpoint_sequence + 1,
            recorded["run"].checkpoint_sequence,
        )
        self.assertEqual((event,), self.execution_store.list_supervision_events(self.run.run_id))

        self.now = T8_30
        replay = self.execution_store.record_supervision_event(
            event=event,
            expected_run_digest=recorded["run"].run_digest,
            source_authority=self.source_authority,
            lease_owner_id=self.owner,
            lease_token=self.token,
        )
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(
            recorded["run"].checkpoint_sequence,
            replay["run"].checkpoint_sequence,
        )

    def test_checkpoint_canonical_tamper_is_detected(self) -> None:
        checkpoint = self.execution_store.latest_checkpoint(self.run.run_id)
        payload = checkpoint.to_dict()
        payload["checkpoint_sequence"] = checkpoint.checkpoint_sequence + 1
        payload["steps_used"] = 1
        payload["checkpoint_digest"] = ""
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "INSERT INTO cognitive_run_checkpoints("
                "run_id,checkpoint_sequence,safe_to_resume,authorization_digest,"
                "last_completed_execution_id,canonical_checkpoint,checkpoint_digest,"
                "recorded_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    self.run.run_id,
                    checkpoint.checkpoint_sequence + 1,
                    int(checkpoint.safe_to_resume),
                    checkpoint.authorization_digest,
                    checkpoint.last_completed_execution_id,
                    canonical_json(payload),
                    "f" * 64,
                    checkpoint.recorded_at,
                ),
            )
            connection.commit()
        with self.assertRaises(OperationalStateError) as tampered:
            self.execution_store.latest_checkpoint(self.run.run_id)
        self.assert_reason(tampered, "cognitive_checkpoint_record_tampered")

    def test_cross_project_acceptance_cannot_complete_the_run(self) -> None:
        self.run = self.transition(self.run, "executing")
        proposal = self.proposal_for()
        self.persist_proposal(proposal)
        reservation = self.reserve(proposal)
        succeeded = self.execution_result(
            proposal,
            reservation,
            suffix="cross-project-prerequisite",
            status="succeeded",
            verification_status="passed",
            reason_codes=("policy_allow",),
        )
        self.execution_store.record_tool_execution(
            result=succeeded,
            source_authority=self.source_authority,
            lease_owner_id=self.owner,
            lease_token=self.token,
        )
        verifying_tool = self.execution_store.get_run(self.run.run_id)
        verified = self.record_independent(
            verifying_tool,
            succeeded,
            result="passed",
            evidence_digest="f" * 64,
        )
        self.run = verified["run"]
        self.run = self.transition(self.run, "verifying")
        self.run = self.transition(self.run, "waiting_acceptance")
        checkpoint_before = self.execution_store.latest_checkpoint(self.run.run_id)

        other_authority = self.make_authority(
            authority_id="authority:v10-cross-project",
            project_id="project:other",
            issued_by="user:other",
        )
        other_anchor = SourceAnchor(
            anchor_id="anchor:v10-cross-project",
            source_kind="user_declaration",
            locator="test:v10-cross-project",
            evidence_digest="b" * 64,
            observed_at=T8,
            privacy_scope="private_metadata",
        )
        other_contract = DelegationContract(
            delegation_id="delegation:v10-cross-project",
            project_id="project:other",
            work_item_id="work-item:v10-cross-project",
            intent="Create a real but foreign accepted outcome.",
            read_scope=("*",),
            write_scope=("*",),
            prohibited_actions=("merge", "tag", "push", "start_next_version"),
            tool_scope=("project.file.read",),
            risk="low",
            valid_from=T8,
            expires_at=T9,
            acceptance_criteria=("The foreign outcome is verified.",),
            rollback="Discard the disposable database.",
            version_scope=("v10-D",),
            commit_permission=False,
            merge_permission=False,
            tag_permission=False,
            push_permission=False,
            next_node_permission=False,
            authority_digest=other_authority.authority_digest,
        )
        other = DelegationService(self.operational, source_anchor=other_anchor)
        other.create(
            contract=other_contract,
            authority=other_authority,
            user_actor_id="user:other",
            lease_owner_id=self.owner,
            lease_token=self.token,
        )
        for state, at, actor_kind, actor_id in (
            ("authorized", "2026-08-01T00:08:01Z", "user", "user:other"),
            ("queued", "2026-08-01T00:08:02Z", "agent", "agent:other"),
            ("running", "2026-08-01T00:08:03Z", "agent", "agent:other"),
        ):
            other.transition(
                delegation_id=other_contract.delegation_id,
                next_state=state,
                authority=other_authority,
                actor_kind=actor_kind,
                actor_id=actor_id,
                at=at,
                lease_owner_id=self.owner,
                lease_token=self.token,
                reason_code=f"test_{state}",
            )
        other.record_execution_result(
            delegation_id=other_contract.delegation_id,
            outcome_id="outcome:v10-cross-project",
            runtime_run_id=self.run.run_id,
            result="completed",
            receipt_digest="c" * 64,
            artifact_references=("artifact:cross-project",),
            authority=other_authority,
            runtime_actor_id="runtime:other",
            at="2026-08-01T00:08:04Z",
            lease_owner_id=self.owner,
            lease_token=self.token,
        )
        other.verify_outcome(
            outcome_id="outcome:v10-cross-project",
            verification_digest="d" * 64,
            passed=True,
            authority=other_authority,
            verifier_id="runtime:other-verifier",
            at="2026-08-01T00:08:05Z",
            lease_owner_id=self.owner,
            lease_token=self.token,
        )
        accepted = other.accept_outcome(
            outcome_id="outcome:v10-cross-project",
            authority=other_authority,
            user_actor_id="user:other",
            at="2026-08-01T00:08:06Z",
            lease_owner_id=self.owner,
            lease_token=self.token,
        )
        with self.assertRaises(OperationalStateError) as foreign:
            self.transition(
                self.run,
                "completed",
                acceptance_event_id=accepted["event_id"],
            )
        self.assert_reason(foreign, "cognitive_run_acceptance_event_invalid")
        current = self.execution_store.get_run(self.run.run_id)
        checkpoint_after = self.execution_store.latest_checkpoint(self.run.run_id)
        self.assertEqual("waiting_acceptance", current.status)
        self.assertEqual(checkpoint_before.checkpoint_digest, checkpoint_after.checkpoint_digest)


if __name__ == "__main__":
    unittest.main()
