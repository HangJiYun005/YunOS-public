from __future__ import annotations

import sqlite3
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from memory.cognitive_authorization import (
    CognitiveAuthorizationError,
    issue_authorization_artifact,
)
from memory.cognitive_cancellation import issue_user_cancellation_artifact
from memory.cognitive_capability import build_suggested_capability_registry
from memory.cognitive_execution_models import (
    TOOL_PROTOCOL_VERSION,
    _create_cognitive_checkpoint,
    _create_cognitive_run,
    _create_tool_execution_preview,
    _create_tool_execution_result,
    create_tool_proposal,
)
from memory.cognitive_execution_store import CognitiveExecutionStore
from memory.cognitive_objective import (
    AuthorityRequest,
    ObjectiveSourceAnchor,
    build_objective_contract,
)
from memory.cognitive_planning import (
    CapabilityRequirement,
    PlanNode,
    build_plan_graph,
)
from memory.cognitive_planning_store import CognitivePlanningStore
from memory.cognitive_provider import build_suggested_provider_registry
from memory.cognitive_provider_store import CognitiveProviderStore
from memory.cognitive_state_migration import apply_cognitive_state_migration
from memory.cognitive_state_store import CognitiveStateStore
from memory.cognitive_supervision import create_supervision_event
from memory.cognitive_verification import (
    CognitiveIndependentVerifier,
    _create_independent_verification,
)
from memory.operational_state_delegation import (
    DelegationContract,
    DelegationService,
)
from memory.operational_state_models import (
    AuthorityArtifact,
    OperationalStateError,
    SourceAnchor,
    build_state_event,
    canonical_digest,
    make_identity,
)
from memory.operational_state_store import (
    OperationalStateStore,
    initialize_operational_database,
)


T0 = "2026-08-01T00:00:00.000000Z"
T1 = "2026-08-01T00:01:00.000000Z"
T2 = "2026-08-01T00:02:00.000000Z"
T3 = "2026-08-01T00:03:00.000000Z"
T4 = "2026-08-01T00:04:00.000000Z"
T5 = "2026-08-01T00:05:00.000000Z"
T9 = "2026-08-01T01:00:00.000000Z"


class _RecoveryFixture:
    """Disposable, provider-free execution-store fixture for recovery gates."""

    def __init__(self) -> None:
        self.temp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp.name).resolve(strict=True)
        self.root_text = str(self.root)
        self.database = self.root / "operational.db"
        (self.root / "README.txt").write_text(
            "bounded recovery fixture\n",
            encoding="utf-8",
        )
        initialize_operational_database(self.database, created_at=T0)
        apply_cognitive_state_migration(self.database, created_at=T1)
        self.now = T2
        self.owner = "owner:v10-d-recovery-test"
        self.lease_token = "v10-d-recovery-test-lease"
        self.operational = OperationalStateStore(self.database)
        self.operational.acquire_writer_lease(
            owner_id=self.owner,
            token=self.lease_token,
            acquired_at=T0,
            expires_at=T9,
        )
        self.anchor = SourceAnchor(
            anchor_id="anchor:v10-d-recovery-test",
            source_kind="user_declaration",
            locator="test:v10-d-recovery-boundaries",
            evidence_digest="a" * 64,
            observed_at=T0,
            privacy_scope="private_metadata",
        )
        self.source_authority = AuthorityArtifact(
            authority_id="authority:v10-d-recovery-test",
            issued_by="user:local-user",
            issued_at=T0,
            expires_at=T4,
            target_project_id="project:yunos",
            allowed_event_types=(
                "authorization_artifact_issued",
                "capability_registry_install",
                "cognitive_run_created",
                "cognitive_run_update",
                "decision_approved",
                "delegation_contract_register",
                "delegation_proposed",
                "plan_created",
                "provider_registry_install",
                "tool_execution_record",
                "tool_execution_reserved",
                "tool_proposal_record",
                "verification_record",
            ),
            read_scope=("*",),
            write_scope=("*",),
            prohibited_operations=(
                "merge",
                "tag",
                "push",
                "create-next-node",
            ),
            commit_permission=True,
            merge_permission=False,
            tag_permission=False,
            push_permission=False,
            next_node_permission=False,
        )
        self.objective = build_objective_contract(
            user_utterance="Exercise one bounded recovery contract.",
            interpreted_goal=(
                "Prove reservation, verification, and expired-authority recovery "
                "fail closed."
            ),
            project_id="project:yunos",
            source_anchors=(
                ObjectiveSourceAnchor(
                    anchor_id=self.anchor.anchor_id,
                    source_kind=self.anchor.source_kind,
                    locator=self.anchor.locator,
                    evidence_digest=self.anchor.evidence_digest,
                    observed_at=self.anchor.observed_at,
                    privacy_scope=self.anchor.privacy_scope,
                ),
            ),
            success_criteria=(
                "Uncertain executions cannot cross a terminal boundary.",
                "Only independent verification can complete a plan node.",
            ),
            prohibited_outcomes=(
                "Do not touch a production database.",
                "Do not call a Provider or the network.",
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
            ),
        )
        self.objective_event = build_state_event(
            project_id=self.objective.project_id,
            entity_id="decision:v10-d-recovery-objective",
            entity_type="decision",
            event_type="decision_approved",
            source_kind="declared",
            source_anchor=self.anchor,
            authority_kind="user",
            actor_kind="user",
            actor_id="user:local-user",
            observed_at=T1,
            effective_at=T1,
            recorded_at=T1,
            expires_at=None,
            supersedes_event_id=None,
            caused_by_event_id=None,
            idempotency_key="idempotency:v10-d-recovery-objective",
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
        CognitiveStateStore(
            self.database,
            trusted_clock=lambda: self.now,
        ).commit_objective(
            contract=self.objective.to_dict(),
            authority=self.source_authority,
            event=self.objective_event,
            lease_owner_id=self.owner,
            lease_token=self.lease_token,
            created_at=T1,
        )

        self.capability_registry = build_suggested_capability_registry(
            {"project.file.read": "available"}
        )
        self.planning_store = CognitivePlanningStore(
            self.database,
            trusted_clock=lambda: self.now,
        )
        self.planning_store.install_builtin_capabilities(
            registry=self.capability_registry,
            authority=self.source_authority,
            lease_owner_id=self.owner,
            lease_token=self.lease_token,
        )
        self.profile = self.capability_registry.lookup(
            "project.file.read",
            version=1,
        )
        self.requirement = CapabilityRequirement(
            capability_id=self.profile.capability_id,
            capability_version=self.profile.version,
            capability_digest=self.profile.capability_digest,
        )
        self.node = PlanNode(
            node_id="node:v10-d-recovery-read",
            objective_id=self.objective.objective_id,
            title="Read one disposable file",
            purpose="Produce one bounded executor observation for verification.",
            capability_requirements=(self.requirement,),
            read_scope=(self.root_text,),
            risk="low",
            reversible=True,
            authority_requirement="read",
            success_criteria=("The exact temporary file is observed.",),
            verification=("Independently recompute the returned SHA-256.",),
            timeout_ms=1_000,
        )
        self.plan = build_plan_graph(
            objective=self.objective,
            nodes=(self.node,),
            change_reason="Create the isolated v10-D recovery plan.",
            created_at=T1,
        )
        self.plan_event = build_state_event(
            project_id=self.objective.project_id,
            entity_id=make_identity(
                "workitem",
                {"plan_id": self.plan.plan_id, "version": self.plan.version},
            ),
            entity_type="work_item",
            event_type="plan_created",
            source_kind="declared",
            source_anchor=self.anchor,
            authority_kind="user",
            actor_kind="agent",
            actor_id="agent:yunos-planner",
            observed_at=T1,
            effective_at=T1,
            recorded_at=T1,
            expires_at=None,
            supersedes_event_id=None,
            caused_by_event_id=self.objective_event.event_id,
            idempotency_key="idempotency:v10-d-recovery-plan",
            conflict_group_id=None,
            payload={
                "state": "proposed",
                "plan_id": self.plan.plan_id,
                "plan_version": self.plan.version,
                "plan_digest": self.plan.plan_digest,
                "objective_id": self.objective.objective_id,
                "objective_version": self.objective.version,
                "objective_digest": self.objective.canonical_digest,
                "authority_impact": self.plan.revisions[-1].authority_impact,
            },
            privacy_scope="private_metadata",
            authority_digest=self.source_authority.authority_digest,
        )
        self.planning_store.commit_plan(
            graph=self.plan,
            authority=self.source_authority,
            event=self.plan_event,
            lease_owner_id=self.owner,
            lease_token=self.lease_token,
        )

        provider_registry = build_suggested_provider_registry(allow_fake=True)
        CognitiveProviderStore(
            self.database,
            trusted_clock=lambda: self.now,
        ).install_builtin_providers(
            registry=provider_registry,
            authority=self.source_authority,
            lease_owner_id=self.owner,
            lease_token=self.lease_token,
        )

        self.delegation = DelegationContract(
            delegation_id="delegation:v10-d-recovery-test",
            project_id=self.objective.project_id,
            work_item_id=self.plan_event.entity_id,
            intent="Exercise only the disposable recovery-boundary fixture.",
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
            tool_scope=("tool:bounded-native-read",),
            risk="low",
            valid_from=T0,
            expires_at=T4,
            acceptance_criteria=(
                "Independent verification is recorded.",
                "Uncertain state closes to recovery_required.",
            ),
            rollback="Discard the temporary database.",
            version_scope=("v10-D",),
            commit_permission=False,
            merge_permission=False,
            tag_permission=False,
            push_permission=False,
            next_node_permission=False,
            authority_digest=self.source_authority.authority_digest,
        )
        DelegationService(
            self.operational,
            source_anchor=self.anchor,
        ).create(
            contract=self.delegation,
            authority=self.source_authority,
            user_actor_id="user:local-user",
            lease_owner_id=self.owner,
            lease_token=self.lease_token,
        )
        self.authorization = issue_authorization_artifact(
            objective=self.objective,
            plan=self.plan,
            delegation=self.delegation,
            source_authority=self.source_authority,
            read_roots=(self.root_text,),
            write_roots=(),
            allowed_capabilities=(self.requirement,),
            denied_capabilities=(),
            allowed_commands=(),
            risk_ceiling="low",
            max_steps=4,
            max_duration_ms=30_000,
            max_model_calls=0,
            max_tokens=0,
            max_cost_microunits=0,
            commit_permission=False,
            database_permission="none",
            valid_from=T1,
            expires_at=T4,
            rollback_policy=(),
            nonce_material="v10-d-recovery-test-nonce",
        )
        self.execution_store = CognitiveExecutionStore(
            self.database,
            trusted_clock=lambda: self.now,
        )
        self.execution_store.commit_authorization(
            authorization=self.authorization,
            source_authority=self.source_authority,
            lease_owner_id=self.owner,
            lease_token=self.lease_token,
        )
        self.run = self.execution_store.create_run(
            authorization=self.authorization,
            source_authority=self.source_authority,
            provider_id="fake",
            provider_profile_version=1,
            run_nonce="v10-d-recovery-run-nonce",
            lease_owner_id=self.owner,
            lease_token=self.lease_token,
        )
        self.run = self.execution_store.transition_run(
            run_id=self.run.run_id,
            expected_run_digest=self.run.run_digest,
            next_status="executing",
            source_authority=self.source_authority,
            lease_owner_id=self.owner,
            lease_token=self.lease_token,
        )
        self.proposal = create_tool_proposal(
            run_id=self.run.run_id,
            plan=self.plan,
            authorization=self.authorization,
            registry=self.capability_registry,
            plan_node_id=self.node.node_id,
            model_proposal={
                "capability_id": self.profile.capability_id,
                "capability_version": self.profile.version,
                "capability_digest": self.profile.capability_digest,
                "typed_arguments": {"relative_path": "README.txt"},
                "reason": "Read the single explicitly authorized test file.",
                "expected_observation": "Return bounded text and its digest.",
            },
            proposed_at=T2,
        )
        self.execution_store.record_tool_proposal(
            proposal=self.proposal,
            source_authority=self.source_authority,
            lease_owner_id=self.owner,
            lease_token=self.lease_token,
        )
        self.run = self.execution_store.get_run(self.run.run_id)
        self.reservation = None
        self.preview = None

    def close(self) -> None:
        self.temp.cleanup()

    def build_preview(self):
        if self.preview is None:
            self.preview = _create_tool_execution_preview(
                preview_id=make_identity(
                    "toolpreview",
                    {
                        "proposal_digest": self.proposal.proposal_digest,
                        "authorization_digest": self.authorization.authority_digest,
                        "node_digest": self.node.node_digest,
                        "attempt_number": 1,
                    },
                ),
                proposal_digest=self.proposal.proposal_digest,
                authorization_digest=self.authorization.authority_digest,
                plan_digest=self.plan.plan_digest,
                node_digest=self.node.node_digest,
                capability_digest=self.profile.capability_digest,
                attempt_number=1,
                invocation_kind="native_call",
                resolved_argv=(),
                working_directory=self.root_text,
                resolved_bindings=(),
                template_id=None,
                template_digest=None,
                effective_timeout_ms=1_000,
                effective_output_bytes=self.profile.max_output_bytes,
                prepared_at=T2,
                expires_at=T4,
            )
        return self.preview

    def reserve_without_execution(self) -> dict:
        if self.reservation is not None:
            return self.reservation
        preview = self.build_preview()
        execution_key = self.execution_store._execution_key(
            proposal=self.proposal,
            node_digest=preview.node_digest,
            attempt_number=preview.attempt_number,
        )
        event = self.execution_store._resource_event(
            source_authority=self.source_authority,
            project_id=self.authorization.project_id,
            entity_id=make_identity(
                "toolreservation",
                {"execution_key": execution_key},
            ),
            event_type="tool_execution_reserved",
            actor_kind="runtime",
            actor_id="runtime:yunos-tool-gateway",
            at=self.now,
            expires_at=self.authorization.expires_at,
            payload={
                "execution_key": execution_key,
                "run_id": self.proposal.run_id,
                "proposal_id": self.proposal.proposal_id,
                "proposal_digest": self.proposal.proposal_digest,
                "node_digest": self.node.node_digest,
                "capability_digest": self.proposal.capability_digest,
                "argument_digest": self.proposal.argument_digest,
                "authorization_digest": self.authorization.authority_digest,
                "attempt_number": 1,
                "preview_id": preview.preview_id,
                "preview_digest": preview.preview_digest,
                "sealed_preview": preview.to_dict(),
                "operation_executed": False,
            },
            idempotency_key=execution_key,
            authority_kind="verified_runtime",
            source_kind="observed",
        )
        event_preview = self.operational.preview_event(
            authority=self.source_authority,
            event=event,
        )
        receipt = self.operational.commit_preview(
            authority=self.source_authority,
            preview=event_preview,
            lease_owner_id=self.owner,
            lease_token=self.lease_token,
        )
        previous = self.execution_store.get_run(self.run.run_id)
        latest = self.execution_store.latest_checkpoint(previous.run_id)
        checkpoint = _create_cognitive_checkpoint(
            run_id=previous.run_id,
            checkpoint_sequence=previous.checkpoint_sequence + 1,
            safe_to_resume=False,
            run_status="executing",
            authorization_digest=previous.authorization_digest,
            objective_digest=previous.objective_digest,
            plan_digest=previous.plan_digest,
            provider_profile_digest=previous.provider_profile_digest,
            current_node_ids=tuple(
                sorted(set((*latest.current_node_ids, self.node.node_id)))
            ),
            completed_node_ids=latest.completed_node_ids,
            model_budget_state=dict(latest.model_budget_state),
            runtime_recovery_bindings=latest.runtime_recovery_bindings,
            proposal_ids=tuple(
                sorted(set((*latest.proposal_ids, self.proposal.proposal_id)))
            ),
            execution_ids=latest.execution_ids,
            idempotency_keys=tuple(
                sorted(set((*latest.idempotency_keys, execution_key)))
            ),
            last_completed_execution_id=latest.last_completed_execution_id,
            unsafe_reason_codes=("tool_execution_reserved_without_terminal",),
            steps_used=previous.steps_used,
            started_at=previous.started_at,
            last_activity_at=self.now,
            ended_at=None,
            failure_code=None,
            recorded_at=self.now,
        )
        current = _create_cognitive_run(
            run_id=previous.run_id,
            objective_id=previous.objective_id,
            objective_version=previous.objective_version,
            objective_digest=previous.objective_digest,
            plan_id=previous.plan_id,
            plan_version=previous.plan_version,
            plan_digest=previous.plan_digest,
            provider_id=previous.provider_id,
            provider_profile_version=previous.provider_profile_version,
            provider_profile_digest=previous.provider_profile_digest,
            authorization_id=previous.authorization_id,
            authorization_digest=previous.authorization_digest,
            status="executing",
            checkpoint_sequence=checkpoint.checkpoint_sequence,
            last_safe_checkpoint=previous.last_safe_checkpoint,
            model_calls_used=previous.model_calls_used,
            tokens_used=previous.tokens_used,
            cost_used_microunits=previous.cost_used_microunits,
            steps_used=previous.steps_used,
            started_at=previous.started_at,
            last_activity_at=self.now,
            ended_at=None,
            failure_code=None,
        )
        with closing(self.operational._connect(write=True)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self.execution_store._insert_checkpoint(connection, checkpoint)
            self.execution_store._update_run_row(
                connection,
                previous=previous,
                current=current,
            )
            connection.commit()
        self.run = current
        self.reservation = {
            "execution_key": execution_key,
            "reservation_event_id": event.event_id,
            "reservation_receipt_digest": receipt.receipt_digest,
            "preview_id": preview.preview_id,
            "preview_digest": preview.preview_digest,
        }
        return self.reservation

    def execution_result(
        self,
        *,
        status: str = "succeeded",
        verification_status: str = "passed",
        reason_codes: tuple[str, ...] = ("executor_self_check",),
        started_at: str = T2,
        finished_at: str = T3,
        reservation_receipt_digest: str | None = None,
        artifact_references: tuple[str, ...] = (),
    ):
        reservation = self.reserve_without_execution()
        receipt_digest = (
            reservation["reservation_receipt_digest"]
            if reservation_receipt_digest is None
            else reservation_receipt_digest
        )
        return _create_tool_execution_result(
            execution_id=make_identity(
                "toolexecution",
                {
                    "execution_key": reservation["execution_key"],
                    "reservation_receipt_digest": receipt_digest,
                },
            ),
            proposal_id=self.proposal.proposal_id,
            proposal_digest=self.proposal.proposal_digest,
            run_id=self.run.run_id,
            idempotency_key=reservation["execution_key"],
            reservation_event_id=reservation["reservation_event_id"],
            reservation_receipt_digest=receipt_digest,
            preview_id=reservation["preview_id"],
            preview_digest=reservation["preview_digest"],
            attempt_number=1,
            invocation_kind="native_call",
            executor_kind="native",
            resolved_argv=(),
            working_directory=self.root_text,
            policy_decision="allow",
            reason_codes=reason_codes,
            started_at=started_at,
            finished_at=finished_at,
            status=status,
            exit_code=None,
            output={
                "relative_path": "README.txt",
                "content_sha256": "b" * 64,
            },
            stdout_digest=None,
            stderr_digest=None,
            artifact_references=artifact_references,
            runtime_execution_reference=None,
            runtime_receipt_digest=None,
            verification_status=verification_status,
        )

    def record_executor_success(self):
        result = self.execution_result()
        self.now = T3
        stored = self.execution_store.record_tool_execution(
            result=result,
            source_authority=self.source_authority,
            lease_owner_id=self.owner,
            lease_token=self.lease_token,
        )
        return stored["execution"]

    def sealed_verification(
        self,
        execution,
        *,
        result: str,
        collision_marker: str,
    ):
        reason_code = f"independent_recovery_fixture_{result}"
        evidence = {
            "verification_protocol_version": 1,
            "tool_protocol_version": TOOL_PROTOCOL_VERSION,
            "result": result,
            "reason_code": reason_code,
            "proposal_digest": self.proposal.proposal_digest,
            "preview_digest": execution.preview_digest,
            "execution_digest": execution.execution_digest,
            "observation": {
                "method": "independent_recovery_fixture_query",
                "collision_marker": collision_marker,
            },
        }
        evidence_digest = canonical_digest(evidence)
        verified_at = execution.finished_at
        return _create_independent_verification(
            verification_id=make_identity(
                "verification",
                {
                    "run_id": self.proposal.run_id,
                    "proposal_digest": self.proposal.proposal_digest,
                    "preview_digest": execution.preview_digest,
                    "execution_digest": execution.execution_digest,
                    "evidence_digest": evidence_digest,
                    "verified_at": verified_at,
                },
            ),
            run_id=self.proposal.run_id,
            objective_id=self.proposal.objective_id,
            plan_node_id=self.proposal.plan_node_id,
            tool_execution_id=execution.execution_id,
            verification_kind=(
                CognitiveIndependentVerifier._verification_kind(
                    self.proposal.capability_id
                )
            ),
            verifier_kind="independent_tool",
            criteria=(
                *CognitiveIndependentVerifier._criteria(
                    self.proposal.capability_id
                ),
                f"observation:{reason_code}",
            ),
            result=result,
            evidence_references=("artifact:test:README.txt",),
            evidence=evidence,
            evidence_digest=evidence_digest,
            verified_at=verified_at,
            acceptance_state="not_requested",
            acceptance_event_id=None,
            proposal_digest=self.proposal.proposal_digest,
            execution_digest=execution.execution_digest,
            preview_digest=execution.preview_digest,
        )

    def enter_verified_no_change_retry_pause(
        self,
        *,
        detail: str = "equivalent_retry_started",
    ):
        execution = self.execution_result(
            status="failed",
            verification_status="failed",
            reason_codes=(
                "suspected_stall",
                "tool_execution_no_owned_change",
            ),
        )
        self.now = T3
        stored = self.execution_store.record_tool_execution(
            result=execution,
            source_authority=self.source_authority,
            lease_owner_id=self.owner,
            lease_token=self.lease_token,
        )
        verifying = self.execution_store.get_run(self.run.run_id)
        verification = self.sealed_verification(
            stored["execution"],
            result="failed",
            collision_marker="verified-no-change-retry",
        )
        verified = self.execution_store.record_independent_tool_verification(
            verification=verification,
            expected_run_digest=verifying.run_digest,
            source_authority=self.source_authority,
            lease_owner_id=self.owner,
            lease_token=self.lease_token,
        )["run"]
        actions = {
            "equivalent_retry_started": (
                "one_equivalent_retry",
                "reuse_same_tool_proposal",
                "no_additional_model_call",
            ),
            "bounded_observation_replan_pending": (
                "persist_bounded_observation",
                "same_objective_plan_node_authority_only",
                "reserve_at_most_one_additional_model_call",
                "no_acceptance",
            ),
        }
        event = create_supervision_event(
            run_id=verified.run_id,
            sequence=1,
            event_type="retry_started",
            severity="warning",
            observed_at=self.now,
            detail=detail,
            evidence={
                "prior_execution_id": execution.execution_id,
                "prior_execution_digest": execution.execution_digest,
                "retry_attempt_number": 2,
                "retryable_reason_codes": ["suspected_stall"],
            },
            action_taken=actions[detail],
            plan_node_id=self.node.node_id,
            tool_execution_id=execution.execution_id,
        )
        supervised = self.execution_store.record_supervision_event(
            event=event,
            expected_run_digest=verified.run_digest,
            source_authority=self.source_authority,
            lease_owner_id=self.owner,
            lease_token=self.lease_token,
        )["run"]
        paused = self.execution_store.transition_run(
            run_id=supervised.run_id,
            expected_run_digest=supervised.run_digest,
            next_status="waiting_external",
            source_authority=self.source_authority,
            lease_owner_id=self.owner,
            lease_token=self.lease_token,
        )
        self.run = paused
        return paused, execution, verification, event

    def verification_rows(self, execution_id: str):
        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            return connection.execute(
                "SELECT verification_id,entity_id,verifier_kind,result,"
                "evidence_digest,verification_digest FROM state_verifications "
                "WHERE json_extract(canonical_verification,"
                "'$.details.tool_execution_id')=? "
                "ORDER BY recorded_at,verification_id",
                (execution_id,),
            ).fetchall()


class V10RecoveryBoundaryTest(unittest.TestCase):
    @contextmanager
    def fixture(self):
        fixture = _RecoveryFixture()
        try:
            yield fixture
        finally:
            fixture.close()

    @staticmethod
    def reason(context) -> str:
        return getattr(context.exception, "reason_code", str(context.exception))

    def test_unresolved_reservation_blocks_forward_transitions(self) -> None:
        for next_status in ("verifying", "waiting_acceptance", "completed"):
            with self.subTest(next_status=next_status), self.fixture() as fixture:
                fixture.reserve_without_execution()
                with self.assertRaises(OperationalStateError) as denied:
                    fixture.execution_store.transition_run(
                        run_id=fixture.run.run_id,
                        expected_run_digest=fixture.run.run_digest,
                        next_status=next_status,
                        source_authority=fixture.source_authority,
                        lease_owner_id=fixture.owner,
                        lease_token=fixture.lease_token,
                    )
                self.assertEqual(
                    "cognitive_run_unresolved_execution_reservation",
                    self.reason(denied),
                )

    def test_unresolved_reservation_closes_failed_or_cancelled_to_recovery(self) -> None:
        for requested_terminal in ("failed", "cancelled"):
            with (
                self.subTest(requested_terminal=requested_terminal),
                self.fixture() as fixture,
            ):
                fixture.reserve_without_execution()
                closed = fixture.execution_store.transition_run(
                    run_id=fixture.run.run_id,
                    expected_run_digest=fixture.run.run_digest,
                    next_status=requested_terminal,
                    failure_code=f"requested_{requested_terminal}",
                    source_authority=fixture.source_authority,
                    lease_owner_id=fixture.owner,
                    lease_token=fixture.lease_token,
                )
                self.assertEqual("recovery_required", closed.status)
                self.assertIsNone(closed.ended_at)
                checkpoint = fixture.execution_store.latest_checkpoint(
                    fixture.run.run_id
                )
                self.assertFalse(checkpoint.safe_to_resume)
                self.assertIn(
                    "tool_execution_reserved_without_terminal",
                    checkpoint.unsafe_reason_codes,
                )

    def test_cancelled_pending_tool_records_exact_reconciliation_evidence(self) -> None:
        with self.fixture() as fixture:
            reservation = fixture.reserve_without_execution()
            closed = fixture.execution_store.transition_run(
                run_id=fixture.run.run_id,
                expected_run_digest=fixture.run.run_digest,
                next_status="cancelled",
                failure_code="user_requested_cancel",
                source_authority=fixture.source_authority,
                lease_owner_id=fixture.owner,
                lease_token=fixture.lease_token,
            )
            before = fixture.execution_store.latest_checkpoint(closed.run_id)
            reconciled = fixture.execution_store.reconcile_reserved_execution(
                reservation_event_id=reservation["reservation_event_id"],
                decision="mark_recovery_required",
                lease_owner_id=fixture.owner,
                lease_token=fixture.lease_token,
            )
            self.assertFalse(reconciled["idempotent_replay"])
            self.assertEqual("recovery_required", reconciled["run"].status)
            self.assertEqual(closed.failure_code, reconciled["run"].failure_code)
            checkpoint = reconciled["checkpoint"]
            self.assertEqual(
                before.checkpoint_sequence + 1,
                checkpoint.checkpoint_sequence,
            )
            self.assertFalse(checkpoint.safe_to_resume)
            self.assertTrue(
                set(before.unsafe_reason_codes).issubset(
                    checkpoint.unsafe_reason_codes
                )
            )
            self.assertIn(
                "tool_reservation_reconciled_no_terminal",
                checkpoint.unsafe_reason_codes,
            )
            bindings = [
                dict(item)
                for item in checkpoint.runtime_recovery_bindings
                if item.get("kind") == "tool_reservation_reconciliation"
            ]
            self.assertEqual(1, len(bindings))
            binding = bindings[0]
            self.assertEqual(
                reservation["reservation_event_id"],
                binding["reservation_event_id"],
            )
            self.assertEqual(
                reservation["reservation_receipt_digest"],
                binding["reservation_receipt_digest"],
            )
            self.assertEqual(
                fixture.proposal.proposal_digest,
                binding["proposal_digest"],
            )
            self.assertEqual(
                reservation["preview_digest"],
                binding["preview_digest"],
            )
            self.assertFalse(binding["effect_recorded"])
            self.assertIsNone(binding["terminal_execution_id"])
            self.assertEqual(
                canonical_digest(
                    {
                        key: value
                        for key, value in binding.items()
                        if key != "reconciliation_digest"
                    }
                ),
                binding["reconciliation_digest"],
            )
            self.assertEqual([], fixture.verification_rows("missing:execution"))

            replay = fixture.execution_store.reconcile_reserved_execution(
                reservation_event_id=reservation["reservation_event_id"],
                decision="mark_recovery_required",
                lease_owner_id=fixture.owner,
                lease_token=fixture.lease_token,
            )
            self.assertTrue(replay["idempotent_replay"])
            self.assertEqual(
                checkpoint.checkpoint_digest,
                replay["checkpoint"].checkpoint_digest,
            )
            self.assertEqual(
                reconciled["reconciliation_digest"],
                replay["reconciliation_digest"],
            )

    def test_verified_no_change_external_pause_resumes_only_while_current(
        self,
    ) -> None:
        with self.fixture() as fixture:
            paused, execution, verification, event = (
                fixture.enter_verified_no_change_retry_pause()
            )
            checkpoint = fixture.execution_store.latest_checkpoint(paused.run_id)
            self.assertTrue(checkpoint.safe_to_resume)
            self.assertEqual((), checkpoint.unsafe_reason_codes)
            self.assertEqual("waiting_external", checkpoint.run_status)
            self.assertEqual(
                execution.execution_id,
                event.tool_execution_id,
            )
            self.assertEqual(
                verification.verification_id,
                fixture.verification_rows(execution.execution_id)[0][0],
            )

            resumed = fixture.execution_store.transition_run(
                run_id=paused.run_id,
                expected_run_digest=paused.run_digest,
                next_status="executing",
                source_authority=fixture.source_authority,
                lease_owner_id=fixture.owner,
                lease_token=fixture.lease_token,
            )
            self.assertEqual("executing", resumed.status)
            self.assertTrue(
                fixture.execution_store.latest_checkpoint(resumed.run_id).safe_to_resume
            )

        with self.fixture() as fixture:
            paused, _execution, _verification, _event = (
                fixture.enter_verified_no_change_retry_pause()
            )
            fixture.now = T5
            with self.assertRaises(CognitiveAuthorizationError) as expired:
                fixture.execution_store.transition_run(
                    run_id=paused.run_id,
                    expected_run_digest=paused.run_digest,
                    next_status="executing",
                    source_authority=fixture.source_authority,
                    lease_owner_id=fixture.owner,
                    lease_token=fixture.lease_token,
                )
            self.assertEqual(
                "authorization_expired_or_not_yet_valid",
                self.reason(expired),
            )
            persisted = fixture.execution_store.get_run(paused.run_id)
            self.assertEqual(paused.run_digest, persisted.run_digest)
            self.assertEqual("waiting_external", persisted.status)

    def test_unknown_external_pause_remains_fail_closed(self) -> None:
        with self.fixture() as fixture:
            paused = fixture.execution_store.transition_run(
                run_id=fixture.run.run_id,
                expected_run_digest=fixture.run.run_digest,
                next_status="waiting_external",
                source_authority=fixture.source_authority,
                lease_owner_id=fixture.owner,
                lease_token=fixture.lease_token,
            )
            checkpoint = fixture.execution_store.latest_checkpoint(paused.run_id)
            self.assertFalse(checkpoint.safe_to_resume)
            self.assertEqual(
                ("cognitive_run_waiting_external",),
                checkpoint.unsafe_reason_codes,
            )
            with self.assertRaises(OperationalStateError) as denied:
                fixture.execution_store.transition_run(
                    run_id=paused.run_id,
                    expected_run_digest=paused.run_digest,
                    next_status="executing",
                    source_authority=fixture.source_authority,
                    lease_owner_id=fixture.owner,
                    lease_token=fixture.lease_token,
                )
            self.assertEqual(
                "cognitive_run_checkpoint_not_safe",
                self.reason(denied),
            )

    def test_verified_no_change_replan_pause_is_safely_resumable(self) -> None:
        with self.fixture() as fixture:
            paused, _execution, _verification, event = (
                fixture.enter_verified_no_change_retry_pause(
                    detail="bounded_observation_replan_pending"
                )
            )
            self.assertEqual(
                "bounded_observation_replan_pending",
                event.detail,
            )
            checkpoint = fixture.execution_store.latest_checkpoint(paused.run_id)
            self.assertTrue(checkpoint.safe_to_resume)
            self.assertEqual((), checkpoint.unsafe_reason_codes)
            resumed = fixture.execution_store.transition_run(
                run_id=paused.run_id,
                expected_run_digest=paused.run_digest,
                next_status="executing",
                source_authority=fixture.source_authority,
                lease_owner_id=fixture.owner,
                lease_token=fixture.lease_token,
            )
            self.assertEqual("executing", resumed.status)

    def test_cancellation_during_pending_verification_preserves_recovery_reasons(
        self,
    ) -> None:
        with self.fixture() as fixture:
            execution = fixture.execution_result()
            fixture.now = T3
            fixture.execution_store.record_tool_execution(
                result=execution,
                source_authority=fixture.source_authority,
                lease_owner_id=fixture.owner,
                lease_token=fixture.lease_token,
            )
            verifying = fixture.execution_store.get_run(fixture.run.run_id)
            before = fixture.execution_store.latest_checkpoint(verifying.run_id)
            self.assertEqual("verifying", verifying.status)
            self.assertEqual(
                ("tool_execution_pending_independent_verification",),
                before.unsafe_reason_codes,
            )
            cancellation = issue_user_cancellation_artifact(
                source_authority=fixture.source_authority,
                project_id=fixture.authorization.project_id,
                run_id=verifying.run_id,
                expected_run_digest=verifying.run_digest,
                session_digest="1" * 64,
                csrf_session_proof_digest="2" * 64,
                requested_at=fixture.now,
                expires_at=T4,
                reason="Cancel while independent verification is pending.",
            )
            cancelled = fixture.execution_store.record_cancellation_request(
                artifact=cancellation,
                source_authority=fixture.source_authority,
                lease_owner_id=fixture.owner,
                lease_token=fixture.lease_token,
            )["run"]
            checkpoint = fixture.execution_store.latest_checkpoint(
                cancelled.run_id
            )
            self.assertEqual("recovery_required", cancelled.status)
            self.assertFalse(checkpoint.safe_to_resume)
            self.assertEqual(
                {
                    "tool_execution_pending_independent_verification",
                    "cancellation_pending_reconciliation",
                },
                set(checkpoint.unsafe_reason_codes),
            )
            self.assertEqual([], fixture.verification_rows(execution.execution_id))
            with self.assertRaises(OperationalStateError) as denied:
                fixture.execution_store.transition_run(
                    run_id=cancelled.run_id,
                    expected_run_digest=cancelled.run_digest,
                    next_status="executing",
                    source_authority=fixture.source_authority,
                    lease_owner_id=fixture.owner,
                    lease_token=fixture.lease_token,
                )
            self.assertEqual(
                "cancellation_latch_blocks_transition",
                self.reason(denied),
            )

    def test_expired_authority_denies_new_reservation_and_normal_execution(self) -> None:
        with self.fixture() as fixture:
            fixture.now = T5
            with self.assertRaises(CognitiveAuthorizationError) as reserve_denied:
                fixture.execution_store.reserve_tool_execution(
                    proposal=fixture.proposal,
                    preview=fixture.build_preview(),
                    source_authority=fixture.source_authority,
                    authorization=fixture.authorization,
                    lease_owner_id=fixture.owner,
                    lease_token=fixture.lease_token,
                )
            self.assertEqual(
                "authorization_expired_or_not_yet_valid",
                self.reason(reserve_denied),
            )

        with self.fixture() as fixture:
            fixture.reserve_without_execution()
            fixture.now = T5
            new_execution = fixture.execution_result(
                started_at=T5,
                finished_at=T5,
            )
            with self.assertRaises(OperationalStateError) as execute_denied:
                fixture.execution_store.record_tool_execution(
                    result=new_execution,
                    source_authority=fixture.source_authority,
                    lease_owner_id=fixture.owner,
                    lease_token=fixture.lease_token,
                )
            self.assertEqual(
                "authority_expired_or_not_yet_valid",
                self.reason(execute_denied),
            )

    def test_expired_authority_allows_only_bound_reservation_reconciliation(self) -> None:
        with self.fixture() as fixture:
            fixture.reserve_without_execution()
            fixture.now = T5
            reconcile = getattr(
                fixture.execution_store,
                "reconcile_reserved_execution",
                None,
            )
            self.assertIsNotNone(
                reconcile,
                "missing expected API CognitiveExecutionStore."
                "reconcile_reserved_execution(reservation_event_id, decision, "
                "observed_result, lease)",
            )
            mismatched = fixture.execution_result(
                status="recovery_required",
                verification_status="unverifiable",
                started_at=T3,
                finished_at=T5,
                reservation_receipt_digest="f" * 64,
            )
            with self.assertRaises(OperationalStateError) as binding_denied:
                reconcile(
                    reservation_event_id=fixture.reservation[
                        "reservation_event_id"
                    ],
                    decision="record_observed_terminal",
                    observed_result=mismatched,
                    lease_owner_id=fixture.owner,
                    lease_token=fixture.lease_token,
                )
            self.assertEqual(
                "tool_execution_reservation_mismatch",
                self.reason(binding_denied),
            )

            terminal = fixture.execution_result(
                status="recovery_required",
                verification_status="unverifiable",
                started_at=T3,
                finished_at=T5,
                artifact_references=("artifact:test:reconciliation-log",),
            )
            reconciled = reconcile(
                reservation_event_id=fixture.reservation[
                    "reservation_event_id"
                ],
                decision="record_observed_terminal",
                observed_result=terminal,
                lease_owner_id=fixture.owner,
                lease_token=fixture.lease_token,
            )
            self.assertEqual(
                terminal.execution_digest,
                reconciled["execution"].execution_digest,
            )
            run = fixture.execution_store.get_run(fixture.run.run_id)
            self.assertEqual("recovery_required", run.status)
            self.assertIsNone(run.ended_at)
            self.assertEqual(
                terminal.execution_digest,
                fixture.execution_store.get_execution_by_idempotency(
                    terminal.idempotency_key
                ).execution_digest,
            )

    def test_executor_self_check_enters_verifying_without_completing_node(self) -> None:
        with self.fixture() as fixture:
            execution = fixture.record_executor_success()
            run = fixture.execution_store.get_run(fixture.run.run_id)
            checkpoint = fixture.execution_store.latest_checkpoint(run.run_id)
            self.assertEqual("passed", execution.verification_status)
            self.assertEqual("verifying", run.status)
            self.assertIn(fixture.node.node_id, checkpoint.current_node_ids)
            self.assertNotIn(fixture.node.node_id, checkpoint.completed_node_ids)
            self.assertEqual([], fixture.verification_rows(execution.execution_id))

    def test_acceptance_is_locked_until_independent_state_verification(self) -> None:
        with self.fixture() as fixture:
            fixture.record_executor_success()
            run = fixture.execution_store.get_run(fixture.run.run_id)
            with self.assertRaises(OperationalStateError) as denied:
                fixture.execution_store.transition_run(
                    run_id=run.run_id,
                    expected_run_digest=run.run_digest,
                    next_status="waiting_acceptance",
                    source_authority=fixture.source_authority,
                    lease_owner_id=fixture.owner,
                    lease_token=fixture.lease_token,
                )
            self.assertIn(
                self.reason(denied),
                {
                    "cognitive_run_checkpoint_not_safe",
                    "cognitive_run_plan_nodes_incomplete",
                    "cognitive_run_independent_verification_incomplete",
                },
            )

    def test_independent_passed_verification_unlocks_node(self) -> None:
        with self.fixture() as fixture:
            execution = fixture.record_executor_success()
            record = getattr(
                fixture.execution_store,
                "record_independent_tool_verification",
                None,
            )
            self.assertIsNotNone(
                record,
                "missing expected API CognitiveExecutionStore."
                "record_independent_tool_verification(verification, "
                "expected_run_digest, source_authority, lease)",
            )
            verification = fixture.sealed_verification(
                execution,
                result="passed",
                collision_marker="e" * 64,
            )
            run = fixture.execution_store.get_run(fixture.run.run_id)
            record(
                verification=verification,
                expected_run_digest=run.run_digest,
                source_authority=fixture.source_authority,
                lease_owner_id=fixture.owner,
                lease_token=fixture.lease_token,
            )
            run = fixture.execution_store.get_run(fixture.run.run_id)
            checkpoint = fixture.execution_store.latest_checkpoint(run.run_id)
            self.assertEqual("executing", run.status)
            self.assertNotIn(fixture.node.node_id, checkpoint.current_node_ids)
            self.assertIn(fixture.node.node_id, checkpoint.completed_node_ids)
            rows = fixture.verification_rows(execution.execution_id)
            self.assertEqual(1, len(rows))
            self.assertEqual("passed", rows[0]["result"])
            self.assertEqual(
                verification.evidence_digest,
                rows[0]["evidence_digest"],
            )
            self.assertNotEqual("executor", rows[0]["verifier_kind"])

    def test_failed_or_unverifiable_independent_verification_fails_closed(self) -> None:
        for result, evidence_digest in (("failed", "f" * 64), ("unverifiable", "e" * 64)):
            with self.subTest(result=result), self.fixture() as fixture:
                execution = fixture.record_executor_success()
                record = getattr(
                    fixture.execution_store,
                    "record_independent_tool_verification",
                    None,
                )
                self.assertIsNotNone(
                    record,
                    "missing expected API CognitiveExecutionStore."
                    "record_independent_tool_verification(verification, "
                    "expected_run_digest, source_authority, lease)",
                )
                verification = fixture.sealed_verification(
                    execution,
                    result=result,
                    collision_marker=evidence_digest,
                )
                run = fixture.execution_store.get_run(fixture.run.run_id)
                record(
                    verification=verification,
                    expected_run_digest=run.run_digest,
                    source_authority=fixture.source_authority,
                    lease_owner_id=fixture.owner,
                    lease_token=fixture.lease_token,
                )
                run = fixture.execution_store.get_run(fixture.run.run_id)
                checkpoint = fixture.execution_store.latest_checkpoint(run.run_id)
                self.assertEqual("recovery_required", run.status)
                self.assertIn(fixture.node.node_id, checkpoint.current_node_ids)
                self.assertNotIn(
                    fixture.node.node_id,
                    checkpoint.completed_node_ids,
                )
                rows = fixture.verification_rows(execution.execution_id)
                self.assertEqual(1, len(rows))
                self.assertEqual(result, rows[0]["result"])


if __name__ == "__main__":
    unittest.main()
