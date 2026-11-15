from __future__ import annotations

import hashlib
import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import tests.test_v10_execution_store as execution_fixture
from memory.cognitive_authorization import issue_authorization_artifact
from memory.cognitive_command_policy import CommandTemplateRegistry
from memory.cognitive_execution_capabilities import (
    build_bounded_execution_capability_registry,
)
from memory.cognitive_execution_models import (
    TOOL_PROTOCOL_VERSION,
    _create_tool_execution_preview,
    _create_tool_execution_result,
    _tool_proposal_from_verified_record,
    create_tool_proposal,
)
from memory.cognitive_execution_store import CognitiveExecutionStore
from memory.cognitive_local_broker import (
    CognitiveLocalBroker,
    CognitiveLocalBrokerError,
)
from memory.cognitive_objective import AuthorityRequest
from memory.cognitive_planning import (
    CapabilityRequirement,
    PlanEdge,
    PlanNode,
    RetryPolicy,
    build_plan_graph,
    pause_plan,
    resume_plan,
)
from memory.cognitive_supervision import ProcessSupervisor, create_supervision_event
from memory.cognitive_tool_gateway import (
    CognitiveToolGateway,
    CognitiveToolGatewayError,
)
from memory.cognitive_verification import CognitiveIndependentVerifier
from memory.operational_state_delegation import DelegationContract, DelegationService
from memory.operational_state_models import canonical_digest, make_identity
import tests.test_v10_model_budget_store as model_fixture


def _sha256(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


class _RollbackExecutionFixture(execution_fixture.CognitiveExecutionStoreTest):
    """Disposable full Store fixture whose only executable node is rollback."""

    def setUp(self) -> None:
        original_builder = execution_fixture.build_objective_contract
        registry = build_bounded_execution_capability_registry(
            {
                "project.file.read": "available",
                "project.file.patch": "available",
                "project.change.rollback": "available",
            }
        )

        def objective_builder(*args, **kwargs):
            request = kwargs["authority_request"]
            kwargs["risk_tolerance"] = "medium"
            kwargs["authority_request"] = replace(
                request,
                write_roots=(self.root_text,),
                capabilities=("project.change.rollback",),
                risk_ceiling="medium",
            )
            return original_builder(*args, **kwargs)

        with (
            patch.object(
                execution_fixture,
                "build_bounded_execution_capability_registry",
                return_value=registry,
            ),
            patch.object(
                execution_fixture,
                "build_objective_contract",
                side_effect=objective_builder,
            ),
        ):
            super().setUp()

    def persist_ready_plan(self):
        self.profile = self.registry.lookup("project.change.rollback", version=1)
        self.requirement = CapabilityRequirement(
            capability_id=self.profile.capability_id,
            capability_version=self.profile.version,
            capability_digest=self.profile.capability_digest,
        )
        gate = PlanNode(
            node_id="node:v10-recovery-user-gate",
            objective_id=self.objective.objective_id,
            title="Confirm the bounded rollback gate",
            purpose="Bind rollback to a persisted user-controlled gate.",
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
            node_id="node:v10-recovery-rollback",
            objective_id=self.objective.objective_id,
            title="Rollback one owned disposable change",
            purpose="Restore only the file bound by an owned backup receipt.",
            dependencies=(gate.node_id,),
            capability_requirements=(self.requirement,),
            read_scope=(self.root_text,),
            write_scope=(self.root_text,),
            risk="medium",
            reversible=True,
            authority_requirement="explicit_act",
            success_criteria=("The original content digest is restored.",),
            verification=("Independently verify backup and current hashes.",),
            rollback=("Stop if the owned receipt cannot be verified.",),
            timeout_ms=2_000,
            retry_policy=RetryPolicy(
                maximum_attempts=1,
                equivalent_retry_limit=0,
                backoff_ms=0,
                retryable_failures=(),
            ),
        )
        edge = PlanEdge(
            edge_id="edge:v10-recovery-gate-rollback",
            from_node_id=gate.node_id,
            to_node_id=action.node_id,
            edge_kind="resume",
            condition=None,
            priority=0,
        )
        self.now = execution_fixture.T3
        graph = build_plan_graph(
            objective=self.objective,
            nodes=(gate, action),
            edges=(edge,),
            change_reason="Create the bounded rollback acceptance plan.",
            created_at=self.now,
            plan_id="plan:v10-execution-store",
        )
        cause = self.commit_plan(
            graph,
            cause=self.objective_event_id,
            actor_kind="agent",
        )
        self.now = execution_fixture.T4
        graph = resume_plan(
            graph,
            objective=self.objective,
            node_ids=(gate.node_id,),
            reason="The user explicitly completed the rollback gate.",
            created_at=self.now,
            created_by="user",
        )
        cause = self.commit_plan(graph, cause=cause, actor_kind="user")
        self.now = execution_fixture.T5
        graph = pause_plan(
            graph,
            objective=self.objective,
            node_ids=(action.node_id,),
            reason="Persist a safe rollback scheduling checkpoint.",
            created_at=self.now,
            created_by="agent",
        )
        cause = self.commit_plan(graph, cause=cause, actor_kind="agent")
        self.now = execution_fixture.T6
        graph = resume_plan(
            graph,
            objective=self.objective,
            node_ids=(action.node_id,),
            reason="The bounded rollback is ready.",
            created_at=self.now,
            created_by="user",
        )
        self.commit_plan(graph, cause=cause, actor_kind="user")
        return graph

    def persist_delegation(self, *, delegation_id: str, work_item_id: str):
        contract = DelegationContract(
            delegation_id=delegation_id,
            project_id=self.objective.project_id,
            work_item_id=work_item_id,
            intent="Rollback one owned change in a disposable project.",
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
            risk="medium",
            valid_from=execution_fixture.T7,
            expires_at=execution_fixture.T_END,
            acceptance_criteria=("The rollback is independently verified.",),
            rollback="Stop without widening the owned artifact boundary.",
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
            write_roots=(self.root_text,),
            allowed_capabilities=(self.requirement,),
            denied_capabilities=(),
            allowed_commands=(),
            risk_ceiling="medium",
            max_steps=1,
            max_duration_ms=1_800_000,
            max_model_calls=1,
            max_tokens=4_096,
            max_cost_microunits=50_000,
            commit_permission=False,
            database_permission="none",
            valid_from=execution_fixture.T8,
            expires_at=execution_fixture.T9,
            rollback_policy=("Restore only the owned backup receipt.",),
            nonce_material=nonce,
        )
        self.now = execution_fixture.T8_20
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


def _new_rollback_fixture(
    method_name: str = "runTest",
    fixture_type=_RollbackExecutionFixture,
):
    return fixture_type(methodName=method_name)


# unittest discovers every TestCase class reachable as a module attribute.  The
# private fixture type is captured by the factory above and then hidden so only
# the acceptance tests below are collected.
del _RollbackExecutionFixture


class V10AgentRecoveryAcceptanceTest(unittest.TestCase):
    """Deterministic recovery and restart acceptance on disposable data only."""

    def setUp(self) -> None:
        self.fixtures: list[unittest.TestCase] = []

    def tearDown(self) -> None:
        for fixture in reversed(self.fixtures):
            fixture.tearDown()

    @staticmethod
    def assert_reason(context, expected: str) -> None:
        actual = getattr(context.exception, "reason_code", str(context.exception))
        if actual != expected:
            raise AssertionError(f"expected reason {expected!r}, got {actual!r}")

    def fixture(self, *, rollback: bool = False):
        fixture_type = (
            _new_rollback_fixture
            if rollback
            else execution_fixture.CognitiveExecutionStoreTest
        )
        fixture = (
            fixture_type("runTest")
            if rollback
            else fixture_type(methodName="runTest")
        )
        fixture.setUp()
        self.fixtures.append(fixture)
        return fixture

    @staticmethod
    def gateway_components(fixture):
        artifact_root = fixture.root / "recovery-artifacts"
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
        return artifact_root, broker, gateway, verifier

    @staticmethod
    def seed_owned_patch(fixture, broker):
        target = fixture.root / "README.txt"
        before = target.read_bytes()
        after = b"owned disposable change\n"
        profile = fixture.registry.lookup("project.file.patch", version=1)
        arguments = profile.validate_input(
            {
                "relative_path": "README.txt",
                "expected_before_sha256": _sha256(before),
                "replacement_content": after.decode("utf-8"),
                "expected_after_sha256": _sha256(after),
            }
        )
        proposal = _tool_proposal_from_verified_record(
            {
                "protocol_version": TOOL_PROTOCOL_VERSION,
                "proposal_id": "toolproposal:v10-recovery-seed",
                "run_id": fixture.run.run_id,
                "objective_id": fixture.objective.objective_id,
                "objective_digest": fixture.objective.canonical_digest,
                "plan_id": fixture.plan.plan_id,
                "plan_version": fixture.plan.version,
                "plan_digest": fixture.plan.plan_digest,
                "plan_node_id": fixture.action.node_id,
                "capability_id": profile.capability_id,
                "capability_version": profile.version,
                "capability_digest": profile.capability_digest,
                "typed_arguments": arguments,
                "argument_digest": canonical_digest(arguments),
                "idempotency_key": "idempotency:v10-recovery-seed",
                "reason": "Create one owned backup in the disposable sandbox.",
                "expected_observation": "A bounded patch and owned backup receipt.",
                "risk_class": profile.risk_class,
                "authorization_id": fixture.authorization.authorization_id,
                "authorization_digest": fixture.authorization.authority_digest,
                "status": "proposed",
                "proposed_at": fixture.now,
                "runtime_execution_reference": None,
                "runtime_receipt_digest": None,
                "proposal_digest": "",
            }
        )
        from memory.cognitive_path_policy import resolve_project_path

        binding = resolve_project_path(
            project_root=fixture.root,
            relative_path="README.txt",
            access_mode="write_file",
        )
        output = broker.patch_file(
            proposal=proposal,
            execution_key="execution:v10-recovery-seed",
            binding=binding,
        )
        return before, after, output, proposal

    @staticmethod
    def verification_details(verification, proposal, preview, result):
        return {
            **verification.to_operational_details(),
            "run_id": proposal.run_id,
            "tool_execution_id": result.execution_id,
            "execution_digest": result.execution_digest,
            "proposal_id": proposal.proposal_id,
            "proposal_digest": proposal.proposal_digest,
            "preview_id": preview.preview_id,
            "preview_digest": preview.preview_digest,
        }

    def test_gateway_rollback_preview_execute_and_independent_store_verification(self):
        fixture = self.fixture(rollback=True)
        _artifacts, broker, gateway, verifier = self.gateway_components(fixture)
        before, after, seed, _seed_proposal = self.seed_owned_patch(fixture, broker)
        self.assertEqual(after, (fixture.root / "README.txt").read_bytes())

        fixture.run = fixture.transition(fixture.run, "executing")
        proposal = create_tool_proposal(
            run_id=fixture.run.run_id,
            plan=fixture.plan,
            authorization=fixture.authorization,
            registry=fixture.registry,
            plan_node_id=fixture.action.node_id,
            model_proposal={
                "capability_id": fixture.profile.capability_id,
                "capability_version": fixture.profile.version,
                "capability_digest": fixture.profile.capability_digest,
                "typed_arguments": {
                    "backup_reference": seed["backup_reference"],
                    "expected_current_sha256": _sha256(after),
                },
                "reason": "Restore only the owned disposable backup.",
                "expected_observation": "The original digest is restored.",
            },
            proposed_at=fixture.now,
        )
        fixture.persist_proposal(proposal)
        preview = gateway.preview(
            proposal=proposal,
            plan=fixture.plan,
            authorization=fixture.authorization,
        )
        self.assertEqual("write_file", preview.resolved_bindings[0]["access_mode"])
        outcome = gateway.execute(
            preview=preview,
            proposal=proposal,
            plan=fixture.plan,
            authorization=fixture.authorization,
            source_authority=fixture.source_authority,
            lease_owner_id=fixture.owner,
            lease_token=fixture.token,
        )
        self.assertEqual("succeeded", outcome.result.status)
        self.assertEqual(before, (fixture.root / "README.txt").read_bytes())
        verifying = fixture.execution_store.get_run(fixture.run.run_id)
        self.assertEqual("verifying", verifying.status)

        verification = verifier.verify(
            proposal=proposal,
            preview=preview,
            result=outcome.result,
            verified_at=fixture.now,
        )
        self.assertEqual("passed", verification.result)
        recorded = fixture.execution_store.record_independent_tool_verification(
            verification=verification,
            expected_run_digest=verifying.run_digest,
            source_authority=fixture.source_authority,
            lease_owner_id=fixture.owner,
            lease_token=fixture.token,
        )
        self.assertEqual("executing", recorded["run"].status)
        self.assertTrue(recorded["run"].last_safe_checkpoint)
        self.assertIn(
            fixture.action.node_id,
            fixture.execution_store.latest_checkpoint(fixture.run.run_id).completed_node_ids,
        )

    def test_rollback_backup_record_tamper_and_path_indirection_fail_closed(self):
        for mutation in ("digest_tamper", "path_indirection"):
            with self.subTest(mutation=mutation):
                fixture = self.fixture(rollback=True)
                artifacts, broker, _gateway, _verifier = self.gateway_components(fixture)
                _before, after, seed, seed_proposal = self.seed_owned_patch(
                    fixture,
                    broker,
                )
                storage_key = canonical_digest(
                    {"backup_reference": seed["backup_reference"]}
                )
                record_path = artifacts / f"{storage_key}.json"
                record = json.loads(record_path.read_text(encoding="utf-8"))
                if mutation == "digest_tamper":
                    record["after_sha256"] = "0" * 64
                else:
                    record["relative_path"] = "../outside.txt"
                    record["before_artifact"] = "../foreign.before.bin"
                record_path.write_text(
                    json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    encoding="utf-8",
                )

                with self.assertRaises(CognitiveLocalBrokerError) as denied:
                    broker.resolve_rollback_binding(
                        proposal=seed_proposal,
                        backup_reference=seed["backup_reference"],
                        expected_current_sha256=_sha256(after),
                    )
                self.assert_reason(denied, "tool_rollback_record_tampered")
                self.assertEqual(after, (fixture.root / "README.txt").read_bytes())
                self.assertFalse((fixture.root.parent / "outside.txt").exists())

    def test_cross_run_backup_replay_fails_preview_execute_and_independent_verify(self):
        fixture = self.fixture(rollback=True)
        _artifacts, broker, gateway, verifier = self.gateway_components(fixture)
        before, after, seed, _seed_proposal = self.seed_owned_patch(fixture, broker)

        fixture.run = fixture.transition(fixture.run, "executing")
        proposal_a = create_tool_proposal(
            run_id=fixture.run.run_id,
            plan=fixture.plan,
            authorization=fixture.authorization,
            registry=fixture.registry,
            plan_node_id=fixture.action.node_id,
            model_proposal={
                "capability_id": fixture.profile.capability_id,
                "capability_version": fixture.profile.version,
                "capability_digest": fixture.profile.capability_digest,
                "typed_arguments": {
                    "backup_reference": seed["backup_reference"],
                    "expected_current_sha256": _sha256(after),
                },
                "reason": "Preview the rollback in its owning run.",
                "expected_observation": "The owned path is bound without mutation.",
            },
            proposed_at=fixture.now,
        )
        fixture.persist_proposal(proposal_a)
        preview_a = gateway.preview(
            proposal=proposal_a,
            plan=fixture.plan,
            authorization=fixture.authorization,
        )

        authorization_b, run_b = fixture.second_run()
        run_b = fixture.transition(run_b, "executing")
        proposal_b = create_tool_proposal(
            run_id=run_b.run_id,
            plan=fixture.plan,
            authorization=authorization_b,
            registry=fixture.registry,
            plan_node_id=fixture.action.node_id,
            model_proposal={
                "capability_id": fixture.profile.capability_id,
                "capability_version": fixture.profile.version,
                "capability_digest": fixture.profile.capability_digest,
                "typed_arguments": {
                    "backup_reference": seed["backup_reference"],
                    "expected_current_sha256": _sha256(after),
                },
                "reason": "Attempt to replay another run's backup reference.",
                "expected_observation": "The cross-run replay is denied.",
            },
            proposed_at=fixture.now,
        )
        fixture.persist_proposal(proposal_b)

        with self.assertRaises(CognitiveLocalBrokerError) as preview_denied:
            gateway.preview(
                proposal=proposal_b,
                plan=fixture.plan,
                authorization=authorization_b,
            )
        self.assert_reason(preview_denied, "tool_rollback_owner_mismatch")

        forged_preview = _create_tool_execution_preview(
            preview_id=make_identity(
                "toolpreview",
                {"cross_run_proposal_digest": proposal_b.proposal_digest},
            ),
            proposal_digest=proposal_b.proposal_digest,
            authorization_digest=authorization_b.authority_digest,
            plan_digest=proposal_b.plan_digest,
            node_digest=fixture.action.node_digest,
            capability_digest=proposal_b.capability_digest,
            attempt_number=1,
            invocation_kind="native_call",
            resolved_argv=(),
            working_directory=str(fixture.root),
            resolved_bindings=preview_a.resolved_bindings,
            template_id=None,
            template_digest=None,
            effective_timeout_ms=preview_a.effective_timeout_ms,
            effective_output_bytes=preview_a.effective_output_bytes,
            prepared_at=preview_a.prepared_at,
            expires_at=preview_a.expires_at,
        )
        with self.assertRaises(CognitiveLocalBrokerError) as execute_denied:
            gateway.execute(
                preview=forged_preview,
                proposal=proposal_b,
                plan=fixture.plan,
                authorization=authorization_b,
                source_authority=fixture.source_authority,
                lease_owner_id=fixture.owner,
                lease_token=fixture.token,
            )
        self.assert_reason(execute_denied, "tool_rollback_owner_mismatch")

        forged_result = _create_tool_execution_result(
            execution_id=make_identity(
                "toolexecution",
                {"cross_run_proposal_digest": proposal_b.proposal_digest},
            ),
            proposal_id=proposal_b.proposal_id,
            proposal_digest=proposal_b.proposal_digest,
            run_id=proposal_b.run_id,
            idempotency_key="execution:cross-run-rollback",
            reservation_event_id=make_identity(
                "stateevent",
                {"cross_run_proposal_digest": proposal_b.proposal_digest},
            ),
            reservation_receipt_digest="a" * 64,
            preview_id=forged_preview.preview_id,
            preview_digest=forged_preview.preview_digest,
            attempt_number=1,
            invocation_kind="native_call",
            executor_kind="local_broker",
            resolved_argv=(),
            working_directory=str(fixture.root),
            policy_decision="allow",
            reason_codes=("tool_execution_verified",),
            started_at=fixture.now,
            finished_at=fixture.now,
            status="succeeded",
            exit_code=None,
            output={
                "backup_reference": seed["backup_reference"],
                "relative_path": "README.txt",
                "restored_sha256": _sha256(before),
                "verification_passed": True,
            },
            stdout_digest=None,
            stderr_digest=None,
            artifact_references=(),
            runtime_execution_reference=None,
            runtime_receipt_digest=None,
            verification_status="passed",
        )
        verification = verifier.verify(
            proposal=proposal_b,
            preview=forged_preview,
            result=forged_result,
            verified_at=fixture.now,
        )
        self.assertEqual("failed", verification.result)
        self.assertIn(
            "observation:independent_rollback_owner_mismatch",
            verification.criteria,
        )
        self.assertEqual(after, (fixture.root / "README.txt").read_bytes())

    def test_equivalent_retry_is_one_only_and_requires_verified_no_change(self):
        fixture = self.fixture()
        fixture.run = fixture.transition(fixture.run, "executing")
        proposal = fixture.proposal_for()
        fixture.persist_proposal(proposal)
        first_reservation = fixture.reserve(proposal)
        first = fixture.execution_result(
            proposal,
            first_reservation,
            suffix="recovery-attempt-one",
            status="failed",
            verification_status="failed",
            reason_codes=("tool_execution_no_owned_change",),
        )
        fixture.execution_store.record_tool_execution(
            result=first,
            source_authority=fixture.source_authority,
            lease_owner_id=fixture.owner,
            lease_token=fixture.token,
        )
        verifying = fixture.execution_store.get_run(fixture.run.run_id)
        with self.assertRaises(Exception) as before_verification:
            fixture.reserve(proposal, attempt_number=2)
        self.assert_reason(before_verification, "tool_reservation_run_not_executing")

        first_verified = fixture.record_independent(
            verifying,
            first,
            result="failed",
            evidence_digest="1" * 64,
        )
        fixture.run = first_verified["run"]
        second_reservation = fixture.reserve(proposal, attempt_number=2)
        second = fixture.execution_result(
            proposal,
            second_reservation,
            suffix="recovery-attempt-two",
            status="failed",
            verification_status="failed",
            reason_codes=("tool_execution_no_owned_change",),
        )
        fixture.execution_store.record_tool_execution(
            result=second,
            source_authority=fixture.source_authority,
            lease_owner_id=fixture.owner,
            lease_token=fixture.token,
        )
        second_verified = fixture.record_independent(
            fixture.execution_store.get_run(fixture.run.run_id),
            second,
            result="failed",
            evidence_digest="2" * 64,
        )
        fixture.run = second_verified["run"]
        _artifacts, _broker, gateway, _verifier = self.gateway_components(fixture)
        with self.assertRaises(CognitiveToolGatewayError) as third:
            gateway.preview(
                proposal=proposal,
                plan=fixture.plan,
                authorization=fixture.authorization,
                attempt_number=3,
            )
        self.assert_reason(third, "invalid_tool_execution_attempt")

        unsafe = self.fixture()
        unsafe.run = unsafe.transition(unsafe.run, "executing")
        unsafe_proposal = unsafe.proposal_for()
        unsafe.persist_proposal(unsafe_proposal)
        unsafe_reservation = unsafe.reserve(unsafe_proposal)
        unsafe_result = unsafe.execution_result(
            unsafe_proposal,
            unsafe_reservation,
            suffix="recovery-unverified-change",
            status="failed",
            verification_status="failed",
            reason_codes=("tool_execution_effect_unknown",),
        )
        unsafe.execution_store.record_tool_execution(
            result=unsafe_result,
            source_authority=unsafe.source_authority,
            lease_owner_id=unsafe.owner,
            lease_token=unsafe.token,
        )
        unsafe_verification = unsafe.record_independent(
            unsafe.execution_store.get_run(unsafe.run.run_id),
            unsafe_result,
            result="failed",
            evidence_digest="3" * 64,
        )
        self.assertEqual("recovery_required", unsafe_verification["run"].status)
        with self.assertRaises(Exception) as denied_retry:
            unsafe.reserve(unsafe_proposal, attempt_number=2)
        self.assert_reason(denied_retry, "tool_reservation_run_not_executing")

    def test_safe_cancel_and_pending_tool_or_model_cancel_boundaries(self):
        safe = self.fixture()
        cancelled = safe.transition(safe.run, "cancelled")
        self.assertEqual("cancelled", cancelled.status)
        self.assertIsNotNone(cancelled.ended_at)
        self.assertTrue(safe.execution_store.latest_checkpoint(safe.run.run_id).safe_to_resume)

        pending_tool = self.fixture()
        pending_tool.run = pending_tool.transition(pending_tool.run, "executing")
        proposal = pending_tool.proposal_for()
        pending_tool.persist_proposal(proposal)
        reserved = pending_tool.reserve(proposal)
        recovered = pending_tool.transition(
            pending_tool.execution_store.get_run(pending_tool.run.run_id),
            "cancelled",
            failure_code="user_requested_cancel",
        )
        self.assertEqual("recovery_required", recovered.status)
        self.assertIsNone(recovered.ended_at)
        self.assertFalse(
            pending_tool.execution_store.latest_checkpoint(recovered.run_id).safe_to_resume
        )

        model = model_fixture.CognitiveModelBudgetStoreTest(
            methodName="test_reserve_is_atomic_cas_and_binds_every_identity"
        )
        model.setUp()
        self.fixtures.append(model)
        reasoning = model.transition(model.run, "reasoning")
        reservation = model.reservation(suffix="cancel-pending-model")
        model_reserved = model.reserve(reasoning, model.authorization, reservation)
        model_recovered = model.transition(
            model_reserved["run"],
            "cancelled",
            failure_code="user_requested_cancel",
        )
        self.assertEqual("recovery_required", model_recovered.status)
        self.assertIsNone(model_recovered.ended_at)
        self.assertFalse(model.store.latest_checkpoint(model_recovered.run_id).safe_to_resume)

    def test_supervision_success_and_overreach_survive_restart_in_sequence(self):
        fixture = self.fixture()
        fixture.run = fixture.transition(fixture.run, "executing")
        checkpoint = fixture.execution_store.latest_checkpoint(fixture.run.run_id)
        success = create_supervision_event(
            run_id=fixture.run.run_id,
            sequence=1,
            event_type="checkpoint",
            severity="info",
            observed_at=fixture.now,
            detail="The bounded step reached a durable safe checkpoint.",
            evidence={"checkpoint_digest": checkpoint.checkpoint_digest},
            action_taken=("checkpoint_persisted",),
            plan_node_id=fixture.action.node_id,
        )
        first = fixture.execution_store.record_supervision_event(
            event=success,
            expected_run_digest=fixture.run.run_digest,
            source_authority=fixture.source_authority,
            lease_owner_id=fixture.owner,
            lease_token=fixture.token,
        )
        overreach = create_supervision_event(
            run_id=fixture.run.run_id,
            sequence=2,
            event_type="overreach_blocked",
            severity="warning",
            observed_at=fixture.now,
            detail="A request outside the authorized capability set was blocked.",
            evidence={
                "capability_id": "git.push",
                "decision": "deny",
                "reason_code": "agent_overreach_blocked",
            },
            action_taken=("deny", "preserve_checkpoint"),
            plan_node_id=fixture.action.node_id,
        )
        second = fixture.execution_store.record_supervision_event(
            event=overreach,
            expected_run_digest=first["run"].run_digest,
            source_authority=fixture.source_authority,
            lease_owner_id=fixture.owner,
            lease_token=fixture.token,
        )

        restarted = CognitiveExecutionStore(
            fixture.database,
            trusted_clock=lambda: fixture.now,
        )
        visible = restarted.list_supervision_events(fixture.run.run_id)
        self.assertEqual((1, 2), tuple(event.sequence for event in visible))
        self.assertEqual(("checkpoint", "overreach_blocked"), tuple(event.event_type for event in visible))
        self.assertEqual(success.event_digest, visible[0].event_digest)
        self.assertEqual(overreach.event_digest, visible[1].event_digest)
        restarted_run = restarted.get_run(fixture.run.run_id)
        self.assertEqual(second["run"].run_digest, restarted_run.run_digest)
        self.assertEqual(
            checkpoint.checkpoint_sequence + 2,
            restarted_run.checkpoint_sequence,
        )


if __name__ == "__main__":
    unittest.main()
