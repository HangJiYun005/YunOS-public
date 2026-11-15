from __future__ import annotations

import inspect
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace

from memory.cognitive_authorization import (
    SystemModelReconciliationAuthority,
    issue_authorization_artifact,
)
from memory.cognitive_execution_store import CognitiveExecutionStore
from memory.cognitive_orchestration import (
    ModelCallReservation,
    ProviderBinding,
    bind_manual_provider_selection,
)
from memory.operational_state_delegation import DelegationService
from memory.cognitive_supervision import create_supervision_event
from memory.operational_state_models import (
    AuthorityArtifact,
    OperationalStateError,
    canonical_digest,
    make_identity,
)
from tests import test_v10_execution_store as execution_fixture


MODEL_RESERVATION_EVENT = "model_call_reserved"
MODEL_SETTLEMENT_EVENT = "model_call_settled"
MODEL_RECOVERY_EVENT = "model_call_recovery_required"
T_ACTIVE = "2026-08-01T00:08:30Z"
T_SETTLED = "2026-08-01T00:09:00Z"
T_EXPIRED = "2026-08-01T01:00:00Z"


class CognitiveModelBudgetStoreTest(unittest.TestCase):
    """Persistence contract for model-call budgets owned by CognitiveRun.

    The current v10-D tree may not expose these two store methods yet.  In that
    case the API-surface test is the single intentional failure and all tests
    that depend on the absent API are skipped.  Once the methods exist, every
    behavioral contract below becomes active without changing this test file.
    """

    REQUIRED_RESERVE_PARAMETERS = {
        "run_id",
        "expected_run_digest",
        "authorization",
        "source_authority",
        "provider_id",
        "provider_profile_version",
        "provider_profile_digest",
        "request_digest",
        "reservation",
        "lease_owner_id",
        "lease_token",
    }
    REQUIRED_SETTLE_PARAMETERS = {
        "run_id",
        "expected_run_digest",
        "authorization",
        "source_authority",
        "reservation",
        "outcome",
        "runtime_execution_reference",
        "runtime_receipt_identity",
        "runtime_receipt_digest",
        "runtime_usage_digest",
        "usage",
        "actual_cost_microunits",
        "reconciliation_only",
        "lease_owner_id",
        "lease_token",
    }

    @classmethod
    def missing_api(cls) -> tuple[str, ...]:
        return tuple(
            name
            for name in (
                "reserve_model_call",
                "settle_model_call",
                "open_model_reconciliation_authority",
                "quarantine_model_reservation",
            )
            if not callable(getattr(CognitiveExecutionStore, name, None))
        )

    def setUp(self) -> None:
        self.fixture = None
        missing = self.missing_api()
        if self._testMethodName != "test_00_persisted_model_budget_api_surface" and missing:
            self.skipTest(
                "production API gap: CognitiveExecutionStore is missing "
                + ", ".join(missing)
            )

        if self._testMethodName == "test_00_persisted_model_budget_api_surface":
            return

        class BudgetFixture(execution_fixture.CognitiveExecutionStoreTest):
            pass

        def make_budget_authority(
            instance,
            *,
            authority_id: str,
            project_id: str,
            issued_by: str,
        ) -> AuthorityArtifact:
            base = execution_fixture.CognitiveExecutionStoreTest.make_authority(
                instance,
                authority_id=authority_id,
                project_id=project_id,
                issued_by=issued_by,
            )
            return replace(
                base,
                allowed_event_types=tuple(
                    sorted(
                        set(
                            (*base.allowed_event_types,
                             MODEL_RESERVATION_EVENT,
                             MODEL_SETTLEMENT_EVENT,
                             MODEL_RECOVERY_EVENT)
                        )
                    )
                ),
                authority_digest="",
            )

        BudgetFixture.make_authority = make_budget_authority
        self.fixture = BudgetFixture(methodName="runTest")
        self.fixture.setUp()
        self.fixture.now = T_ACTIVE
        self.store = self.fixture.execution_store
        self.authorization = self.fixture.authorization
        self.run = self.fixture.run
        self.profile = self.fixture.provider_registry.get("fake", 1)
        self.provider_binding = bind_manual_provider_selection(
            self.fixture.provider_registry.manual_select(
                provider_id=self.profile.provider_id,
                profile_version=self.profile.profile_version,
                profile_digest=self.profile.profile_digest,
            )
        )

    def tearDown(self) -> None:
        if self.fixture is not None:
            self.fixture.tearDown()

    @staticmethod
    def assert_reason(context, expected: str) -> None:
        actual = getattr(context.exception, "reason_code", str(context.exception))
        if actual != expected:
            raise AssertionError(f"expected reason {expected!r}, got {actual!r}")

    def test_00_persisted_model_budget_api_surface(self) -> None:
        missing = self.missing_api()
        self.assertEqual(
            (),
            missing,
            "production API gap: CognitiveExecutionStore must implement "
            "reserve_model_call(...) and settle_model_call(...)",
        )
        reserve_parameters = set(
            inspect.signature(CognitiveExecutionStore.reserve_model_call).parameters
        )
        settle_parameters = set(
            inspect.signature(CognitiveExecutionStore.settle_model_call).parameters
        )
        open_parameters = set(
            inspect.signature(
                CognitiveExecutionStore.open_model_reconciliation_authority
            ).parameters
        )
        quarantine_parameters = set(
            inspect.signature(
                CognitiveExecutionStore.quarantine_model_reservation
            ).parameters
        )
        self.assertFalse(
            self.REQUIRED_RESERVE_PARAMETERS - reserve_parameters,
            "reserve_model_call contract is missing bound inputs: "
            f"{sorted(self.REQUIRED_RESERVE_PARAMETERS - reserve_parameters)}",
        )
        self.assertFalse(
            self.REQUIRED_SETTLE_PARAMETERS - settle_parameters,
            "settle_model_call contract is missing v8 reconciliation inputs: "
            f"{sorted(self.REQUIRED_SETTLE_PARAMETERS - settle_parameters)}",
        )
        self.assertTrue(
            {"reservation_event_id", "lease_owner_id", "lease_token"}
            .issubset(open_parameters)
        )
        self.assertTrue(
            {
                "reconciliation_authority",
                "expected_run_digest",
                "lease_owner_id",
                "lease_token",
            }.issubset(quarantine_parameters)
        )
        self.assertTrue(
            {
                "authorization",
                "source_authority",
                "reservation",
                "outcome",
                "runtime_receipt_digest",
                "runtime_usage_digest",
                "usage",
                "actual_cost_microunits",
            }.isdisjoint(quarantine_parameters)
        )

    def transition(self, run, next_status: str, **kwargs):
        return self.store.transition_run(
            run_id=run.run_id,
            expected_run_digest=run.run_digest,
            next_status=next_status,
            source_authority=self.fixture.source_authority,
            lease_owner_id=self.fixture.owner,
            lease_token=self.fixture.token,
            **kwargs,
        )

    def reservation(
        self,
        *,
        suffix: str,
        run=None,
        sequence: int = 1,
        input_tokens: int = 2_000,
        output_tokens: int = 512,
        cost_microunits: int = 10_000,
        provider_binding: ProviderBinding | None = None,
    ) -> ModelCallReservation:
        binding = provider_binding or self.provider_binding
        selected_run = run or self.run
        request_digest = canonical_digest(
            {
                "run_id": selected_run.run_id,
                "suffix": suffix,
                "provider_binding_digest": binding.binding_digest,
            }
        )
        return ModelCallReservation(
            reservation_id=make_identity(
                "modelres",
                {
                    "request_digest": request_digest,
                    "sequence": sequence,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cost_microunits": cost_microunits,
                },
            ),
            request_digest=request_digest,
            provider_binding=binding,
            input_tokens=input_tokens,
            output_token_ceiling=output_tokens,
            cost_microunits_ceiling=cost_microunits,
            sequence=sequence,
        )

    def reserve(self, run, authorization, reservation):
        return self.store.reserve_model_call(
            run_id=run.run_id,
            expected_run_digest=run.run_digest,
            authorization=authorization,
            source_authority=self.fixture.source_authority,
            provider_id=self.profile.provider_id,
            provider_profile_version=self.profile.profile_version,
            provider_profile_digest=self.profile.profile_digest,
            request_digest=reservation.request_digest,
            reservation=reservation,
            lease_owner_id=self.fixture.owner,
            lease_token=self.fixture.token,
        )

    @staticmethod
    def usage(
        *, prompt_tokens: int = 300, completion_tokens: int = 100
    ) -> dict[str, int]:
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }

    def settle(
        self,
        reserved,
        reservation,
        *,
        usage: dict[str, int] | None = None,
        cost_microunits: int | None = 9_000,
        outcome: str = "succeeded",
        receipt_identity: str | None = "rrc_v10_model_budget_0001",
        receipt_digest: str | None = "b" * 64,
        reconciliation_only: bool = False,
    ):
        usage_digest = None if usage is None else canonical_digest(usage)
        return self.store.settle_model_call(
            run_id=reserved["run"].run_id,
            expected_run_digest=reserved["run"].run_digest,
            authorization=self.authorization,
            source_authority=self.fixture.source_authority,
            reservation=reservation,
            outcome=outcome,
            runtime_execution_reference=(
                f"model-run:v8:{reservation.reservation_id}"
            ),
            runtime_receipt_identity=receipt_identity,
            runtime_receipt_digest=receipt_digest,
            runtime_usage_digest=usage_digest,
            usage=usage,
            actual_cost_microunits=cost_microunits,
            reconciliation_only=reconciliation_only,
            lease_owner_id=self.fixture.owner,
            lease_token=self.fixture.token,
        )

    def open_model_reconciliation(self, reserved, *, ttl_ms: int = 300_000):
        return self.store.open_model_reconciliation_authority(
            reservation_event_id=reserved["reservation_event_id"],
            lease_owner_id=self.fixture.owner,
            lease_token=self.fixture.token,
            ttl_ms=ttl_ms,
        )

    def quarantine(self, reserved, authority):
        return self.store.quarantine_model_reservation(
            reconciliation_authority=authority,
            expected_run_digest=reserved["run"].run_digest,
            lease_owner_id=self.fixture.owner,
            lease_token=self.fixture.token,
        )

    @staticmethod
    def budget_state(
        reservation: ModelCallReservation,
        *,
        settled: bool,
        usage: dict[str, int] | None = None,
        cost_microunits: int = 0,
    ) -> dict:
        actual = usage or {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
        return {
            "calls_reserved": 1,
            "input_tokens_reserved": reservation.input_tokens,
            "output_tokens_reserved": reservation.output_token_ceiling,
            "cost_microunits_reserved": reservation.cost_microunits_ceiling,
            "request_digests": [reservation.request_digest],
            "settled_reservation_ids": (
                [reservation.reservation_id] if settled else []
            ),
            "actual_input_tokens": actual["prompt_tokens"],
            "actual_output_tokens": actual["completion_tokens"],
            "actual_cost_microunits": cost_microunits,
            "unknown_cost_reserved_ceiling_microunits": 0,
            "unknown_cost_reservation_ids": [],
        }

    def issue_run_with_call_budget(self, maximum_model_calls: int, suffix: str):
        delegation = self.fixture.persist_delegation(
            delegation_id=f"delegation:v10-model-budget-{suffix}",
            work_item_id=f"work-item:v10-model-budget-{suffix}",
        )
        authorization = issue_authorization_artifact(
            objective=self.fixture.objective,
            plan=self.fixture.plan,
            delegation=delegation,
            source_authority=self.fixture.source_authority,
            read_roots=(self.fixture.root_text,),
            write_roots=(),
            allowed_capabilities=(self.fixture.requirement,),
            denied_capabilities=(),
            allowed_commands=(),
            risk_ceiling="low",
            max_steps=2,
            max_duration_ms=1_800_000,
            max_model_calls=maximum_model_calls,
            max_tokens=4_096,
            max_cost_microunits=50_000,
            commit_permission=False,
            database_permission="none",
            valid_from="2026-08-01T00:08:00Z",
            expires_at=T_EXPIRED,
            rollback_policy=(),
            nonce_material=f"v10-model-budget-authorization-{suffix}",
        )
        self.fixture.now = "2026-08-01T00:08:20Z"
        self.store.commit_authorization(
            authorization=authorization,
            source_authority=self.fixture.source_authority,
            lease_owner_id=self.fixture.owner,
            lease_token=self.fixture.token,
        )
        self.fixture.now = T_ACTIVE
        run = self.store.create_run(
            authorization=authorization,
            source_authority=self.fixture.source_authority,
            provider_id="fake",
            provider_profile_version=1,
            run_nonce=f"v10-model-budget-run-{suffix}",
            lease_owner_id=self.fixture.owner,
            lease_token=self.fixture.token,
        )
        return delegation, authorization, run

    def test_reserve_is_atomic_cas_and_binds_every_identity(self) -> None:
        queued = self.run
        reasoning = self.transition(queued, "reasoning")
        reservation = self.reservation(suffix="atomic-cas")
        with self.assertRaises(OperationalStateError) as stale:
            self.reserve(queued, self.authorization, reservation)
        self.assert_reason(stale, "cognitive_run_projection_conflict")
        valid = self.reserve(reasoning, self.authorization, reservation)
        self.assertEqual("reserved", valid["status"])
        self.assertFalse(valid["idempotent_replay"])
        self.assertEqual(reservation.reservation_id, valid["reservation_id"])
        self.assertEqual(64, len(valid["reservation_digest"]))
        self.assertEqual(1, valid["run"].model_calls_used)
        self.assertEqual(0, valid["run"].tokens_used)
        self.assertEqual(0, valid["run"].cost_used_microunits)
        self.assertEqual(
            self.budget_state(reservation, settled=False),
            valid["checkpoint"].to_dict()["model_budget_state"],
        )
        with closing(sqlite3.connect(self.fixture.database)) as connection:
            self.assertEqual(
                1,
                connection.execute(
                    "SELECT COUNT(*) FROM state_events "
                    "WHERE event_type=? AND json_extract(canonical_payload,'$.run_id')=?",
                    (MODEL_RESERVATION_EVENT, self.run.run_id),
                ).fetchone()[0],
            )

    def test_authorization_call_token_and_cost_budgets_are_strict(self) -> None:
        reasoning = self.transition(self.run, "reasoning")
        first = self.reservation(suffix="call-one", sequence=1)
        first_reserved = self.reserve(reasoning, self.authorization, first)
        first_settled = self.settle(first_reserved, first, usage=self.usage())
        call_over = self.reservation(suffix="call-over", sequence=2)
        with self.assertRaises(OperationalStateError) as calls:
            self.reserve(first_settled["run"], self.authorization, call_over)
        self.assert_reason(calls, "model_call_budget_exhausted")

        _token_delegation, token_authorization, token_run = (
            self.issue_run_with_call_budget(1, "token-limit")
        )
        token_run = self.transition(token_run, "reasoning")
        token_over = self.reservation(
            suffix="token-over",
            run=token_run,
            input_tokens=4_096,
            output_tokens=1,
        )
        with self.assertRaises(OperationalStateError) as tokens:
            self.reserve(token_run, token_authorization, token_over)
        self.assert_reason(tokens, "model_token_budget_exhausted")
        self.assertEqual(0, self.store.get_run(token_run.run_id).model_calls_used)

        _cost_delegation, cost_authorization, cost_run = (
            self.issue_run_with_call_budget(1, "cost-limit")
        )
        cost_run = self.transition(cost_run, "reasoning")
        cost_over = self.reservation(
            suffix="cost-over",
            run=cost_run,
            cost_microunits=50_001,
        )
        with self.assertRaises(OperationalStateError) as cost:
            self.reserve(cost_run, cost_authorization, cost_over)
        self.assert_reason(cost, "model_cost_budget_exhausted")
        self.assertEqual(0, self.store.get_run(cost_run.run_id).model_calls_used)

    def test_unsettled_reservation_blocks_second_call_completion_and_resume(self) -> None:
        _delegation, authorization, run = self.issue_run_with_call_budget(2, "pending")
        run = self.transition(run, "executing")
        first = self.reservation(suffix="pending-first", run=run, sequence=1)
        reserved = self.reserve(run, authorization, first)
        second = self.reservation(suffix="pending-second", run=run, sequence=2)
        with self.assertRaises(OperationalStateError) as blocked_second:
            self.reserve(reserved["run"], authorization, second)
        self.assert_reason(blocked_second, "cognitive_model_reservation_pending")

        recovery = self.transition(
            reserved["run"],
            "recovery_required",
            failure_code="model_call_outcome_unknown",
        )
        with self.assertRaises(OperationalStateError) as blocked_resume:
            self.transition(recovery, "executing")
        self.assert_reason(
            blocked_resume,
            "cognitive_run_unresolved_model_reservation",
        )

        completion_delegation, completion_authorization, completion_run = (
            self.issue_run_with_call_budget(2, "pending-completion")
        )
        completion_run = self.transition(completion_run, "executing")
        completion_reservation = self.reservation(
            suffix="pending-completion-first",
            run=completion_run,
            sequence=1,
        )
        completion_reserved = self.reserve(
            completion_run,
            completion_authorization,
            completion_reservation,
        )
        accepted_event_id = self.accept_outcome(
            delegation=completion_delegation,
            run_id=completion_run.run_id,
            suffix="pending-completion",
        )
        with self.assertRaises(OperationalStateError) as blocked_completion:
            self.transition(completion_reserved["run"], "verifying")
        self.assert_reason(
            blocked_completion,
            "cognitive_run_unresolved_model_reservation",
        )

    def accept_outcome(self, *, delegation, run_id: str, suffix: str) -> str:
        service = DelegationService(
            self.fixture.operational,
            source_anchor=self.fixture.anchor,
        )
        service.record_execution_result(
            delegation_id=delegation.delegation_id,
            outcome_id=f"outcome:v10-model-budget-{suffix}",
            runtime_run_id=run_id,
            result="completed",
            receipt_digest="c" * 64,
            artifact_references=("artifact:v10-model-budget",),
            authority=self.fixture.source_authority,
            runtime_actor_id="runtime:v8-model-budget",
            at="2026-08-01T00:09:01Z",
            lease_owner_id=self.fixture.owner,
            lease_token=self.fixture.token,
        )
        service.verify_outcome(
            outcome_id=f"outcome:v10-model-budget-{suffix}",
            verification_digest="d" * 64,
            passed=True,
            authority=self.fixture.source_authority,
            verifier_id="runtime:model-budget-verifier",
            at="2026-08-01T00:09:02Z",
            lease_owner_id=self.fixture.owner,
            lease_token=self.fixture.token,
        )
        accepted = service.accept_outcome(
            outcome_id=f"outcome:v10-model-budget-{suffix}",
            authority=self.fixture.source_authority,
            user_actor_id=self.fixture.source_authority.issued_by,
            at="2026-08-01T00:09:03Z",
            lease_owner_id=self.fixture.owner,
            lease_token=self.fixture.token,
        )
        return accepted["event_id"]

    def test_settle_binds_v8_receipt_usage_and_updates_run_checkpoint_atomically(self) -> None:
        reasoning = self.transition(self.run, "reasoning")
        reservation = self.reservation(suffix="settle")
        reserved = self.reserve(reasoning, self.authorization, reservation)
        usage = self.usage()
        self.fixture.now = T_SETTLED
        settled = self.settle(reserved, reservation, usage=usage)
        self.assertEqual("settled", settled["status"])
        self.assertFalse(settled["idempotent_replay"])
        self.assertEqual(1, settled["run"].model_calls_used)
        self.assertEqual(usage["total_tokens"], settled["run"].tokens_used)
        self.assertEqual(9_000, settled["run"].cost_used_microunits)
        self.assertEqual(
            self.budget_state(
                reservation,
                settled=True,
                usage=usage,
                cost_microunits=9_000,
            ),
            settled["checkpoint"].to_dict()["model_budget_state"],
        )
        self.assertEqual("b" * 64, settled["runtime_receipt_digest"])
        self.assertEqual(canonical_digest(usage), settled["runtime_usage_digest"])
        with self.assertRaises(OperationalStateError) as terminal:
            self.open_model_reconciliation(reserved)
        self.assert_reason(
            terminal,
            "model_reconciliation_reservation_already_settled",
        )

    def test_same_receipt_is_idempotent_but_different_usage_collides(self) -> None:
        reasoning = self.transition(self.run, "reasoning")
        reservation = self.reservation(suffix="receipt-replay")
        reserved = self.reserve(reasoning, self.authorization, reservation)
        usage = self.usage()
        first = self.settle(reserved, reservation, usage=usage)
        replay = self.settle(reserved, reservation, usage=usage)
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(first["settlement_digest"], replay["settlement_digest"])
        with self.assertRaises(OperationalStateError) as collision:
            self.settle(
                reserved,
                reservation,
                usage=self.usage(prompt_tokens=301, completion_tokens=100),
            )
        self.assert_reason(collision, "model_call_settlement_replay_collision")

    def test_persisted_provider_context_limit_is_enforced_before_send(self) -> None:
        authorization = issue_authorization_artifact(
            objective=self.fixture.objective,
            plan=self.fixture.plan,
            delegation=self.fixture.delegation,
            source_authority=self.fixture.source_authority,
            read_roots=(self.fixture.root_text,),
            write_roots=(),
            allowed_capabilities=(self.fixture.requirement,),
            denied_capabilities=(),
            allowed_commands=(),
            risk_ceiling="low",
            max_steps=2,
            max_duration_ms=1_800_000,
            max_model_calls=1,
            max_tokens=8_192,
            max_cost_microunits=50_000,
            commit_permission=False,
            database_permission="none",
            valid_from="2026-08-01T00:08:00Z",
            expires_at="2026-08-01T01:00:00Z",
            rollback_policy=(),
            nonce_material="v10-provider-context-limit-authorization",
        )
        self.store.commit_authorization(
            authorization=authorization,
            source_authority=self.fixture.source_authority,
            lease_owner_id=self.fixture.owner,
            lease_token=self.fixture.token,
        )
        run = self.store.create_run(
            authorization=authorization,
            source_authority=self.fixture.source_authority,
            provider_id=self.profile.provider_id,
            provider_profile_version=self.profile.profile_version,
            run_nonce="v10-provider-context-limit-run",
            lease_owner_id=self.fixture.owner,
            lease_token=self.fixture.token,
        )
        reasoning = self.transition(run, "reasoning")
        reservation = self.reservation(
            suffix="provider-context-limit",
            run=reasoning,
            input_tokens=4_000,
            output_tokens=512,
        )

        with self.assertRaises(OperationalStateError) as exceeded:
            self.reserve(reasoning, authorization, reservation)

        self.assert_reason(exceeded, "provider_context_limit_exceeded")
        checkpoint = self.store.latest_checkpoint(reasoning.run_id)
        self.assertEqual(0, checkpoint.model_budget_state["calls_reserved"])

    def test_unknown_outcome_is_quarantined_with_conservative_budget(self) -> None:
        reasoning = self.transition(self.run, "reasoning")
        reservation = self.reservation(suffix="unknown")
        reserved = self.reserve(reasoning, self.authorization, reservation)
        with self.assertRaises(OperationalStateError) as scalar_unknown:
            self.settle(
                reserved,
                reservation,
                usage=None,
                cost_microunits=None,
                outcome="unknown",
                receipt_identity=None,
                receipt_digest=None,
            )
        self.assert_reason(
            scalar_unknown,
            "model_unknown_requires_system_quarantine",
        )
        unknown = self.quarantine(
            reserved,
            self.open_model_reconciliation(reserved),
        )
        self.assertEqual("quarantined", unknown["status"])
        self.assertEqual("recovery_required", unknown["run"].status)
        self.assertEqual("model_call_outcome_unknown", unknown["reason_code"])
        self.assertFalse(unknown["provider_call_allowed"])
        budget = unknown["checkpoint"].to_dict()["model_budget_state"]
        self.assertEqual(
            [reservation.reservation_id],
            budget["settled_reservation_ids"],
        )
        self.assertEqual(
            [reservation.reservation_id],
            budget["unknown_cost_reservation_ids"],
        )
        self.assertEqual(
            reservation.cost_microunits_ceiling,
            budget["unknown_cost_reserved_ceiling_microunits"],
        )
        self.assertEqual(reservation.input_tokens, budget["actual_input_tokens"])
        self.assertEqual(
            reservation.output_token_ceiling,
            budget["actual_output_tokens"],
        )
        self.assertEqual(
            reservation.cost_microunits_ceiling,
            unknown["run"].cost_used_microunits,
        )
        with closing(sqlite3.connect(self.fixture.database)) as connection:
            terminal_counts = connection.execute(
                "SELECT event_type,COUNT(*) FROM state_events "
                "WHERE event_type IN (?,?) GROUP BY event_type",
                (MODEL_SETTLEMENT_EVENT, MODEL_RECOVERY_EVENT),
            ).fetchall()
        self.assertEqual([], terminal_counts)

        self.fixture.now = T_EXPIRED
        with self.assertRaises(OperationalStateError) as expired_new:
            self.reserve(
                unknown["run"],
                self.authorization,
                self.reservation(suffix="expired-new", sequence=2),
            )
        self.assert_reason(expired_new, "authorization_expired_or_not_yet_valid")

    def test_expired_user_authority_uses_only_sealed_quarantine_authority(self) -> None:
        reasoning = self.transition(self.run, "reasoning")
        reservation = self.reservation(suffix="expired-quarantine")
        reserved = self.reserve(reasoning, self.authorization, reservation)
        self.fixture.now = T_EXPIRED
        with self.assertRaises(OperationalStateError) as ordinary_escape:
            self.settle(
                reserved,
                reservation,
                usage=self.usage(),
                reconciliation_only=True,
            )
        self.assert_reason(
            ordinary_escape,
            "model_reconciliation_system_authority_required",
        )
        with self.assertRaises(OperationalStateError) as expired_unknown:
            self.settle(
                reserved,
                reservation,
                usage=None,
                cost_microunits=None,
                outcome="unknown",
                receipt_identity=None,
                receipt_digest=None,
            )
        self.assert_reason(
            expired_unknown,
            "model_unknown_requires_system_quarantine",
        )
        authority = self.open_model_reconciliation(reserved)
        self.assertIs(type(authority), SystemModelReconciliationAuthority)
        self.assertEqual(
            ("quarantine_model_reservation",),
            authority.allowed_operations,
        )
        with self.assertRaises(OperationalStateError) as expanded:
            authority.authorize("settle_model_call", self.fixture.now)
        self.assert_reason(expanded, "model_reconciliation_scope_violation")
        quarantined = self.quarantine(reserved, authority)
        self.assertEqual("quarantined", quarantined["status"])
        self.assertEqual("recovery_required", quarantined["run"].status)
        self.assertFalse(quarantined["provider_call_allowed"])

    def test_model_reconciliation_authority_rejects_forgery_and_receipt_tamper(
        self,
    ) -> None:
        reasoning = self.transition(self.run, "reasoning")
        reservation = self.reservation(suffix="authority-tamper")
        reserved = self.reserve(reasoning, self.authorization, reservation)
        authority = self.open_model_reconciliation(reserved)
        with self.assertRaises(OperationalStateError) as reconstructed:
            replace(authority, authority_digest="")
        self.assert_reason(
            reconstructed,
            "system_model_reconciliation_authority_factory_required",
        )
        object.__setattr__(authority, "reservation_digest", "f" * 64)
        with self.assertRaises(OperationalStateError) as mutated:
            self.quarantine(reserved, authority)
        self.assert_reason(
            mutated,
            "model_reconciliation_authority_digest_mismatch",
        )

        with closing(sqlite3.connect(self.fixture.database)) as connection:
            connection.execute("DROP TRIGGER state_receipts_no_update")
            connection.execute(
                "UPDATE state_receipts SET receipt_digest=? WHERE event_id=?",
                ("e" * 64, reserved["reservation_event_id"]),
            )
            connection.commit()
        with self.assertRaises(OperationalStateError) as receipt_tamper:
            self.open_model_reconciliation(reserved)
        self.assert_reason(receipt_tamper, "operational_schema_drift")

    def test_model_quarantine_replay_concurrency_and_terminal_lock(self) -> None:
        reasoning = self.transition(self.run, "reasoning")
        reservation = self.reservation(suffix="quarantine-concurrency")
        reserved = self.reserve(reasoning, self.authorization, reservation)
        authority = self.open_model_reconciliation(reserved)

        def quarantine_once(_index: int):
            return self.quarantine(reserved, authority)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(quarantine_once, range(2)))
        self.assertEqual(
            [False, True],
            sorted(item["idempotent_replay"] for item in results),
        )
        self.assertEqual(
            1,
            len(
                {
                    item["quarantine_digest"]
                    for item in results
                }
            ),
        )
        current = next(item for item in results if not item["idempotent_replay"])
        with self.assertRaises(OperationalStateError) as false_success:
            self.settle(
                {"run": current["run"]},
                reservation,
                usage=self.usage(),
            )
        self.assert_reason(false_success, "model_settlement_replay")

    def test_model_quarantine_authority_expiry_and_restart_persistence(self) -> None:
        reasoning = self.transition(self.run, "reasoning")
        reservation = self.reservation(suffix="quarantine-restart")
        reserved = self.reserve(reasoning, self.authorization, reservation)
        short = self.open_model_reconciliation(reserved, ttl_ms=1_000)
        self.fixture.now = "2026-08-01T00:08:32Z"
        with self.assertRaises(OperationalStateError) as expired:
            self.quarantine(reserved, short)
        self.assert_reason(expired, "model_reconciliation_authority_expired")

        restarted = CognitiveExecutionStore(
            self.fixture.database,
            trusted_clock=lambda: self.fixture.now,
        )
        fresh = restarted.open_model_reconciliation_authority(
            reservation_event_id=reserved["reservation_event_id"],
            lease_owner_id=self.fixture.owner,
            lease_token=self.fixture.token,
        )
        quarantined = restarted.quarantine_model_reservation(
            reconciliation_authority=fresh,
            expected_run_digest=reserved["run"].run_digest,
            lease_owner_id=self.fixture.owner,
            lease_token=self.fixture.token,
        )
        reloaded = CognitiveExecutionStore(
            self.fixture.database,
            trusted_clock=lambda: self.fixture.now,
        )
        loaded_run = reloaded.get_run(self.run.run_id)
        loaded_checkpoint = reloaded.latest_checkpoint(self.run.run_id)
        self.assertEqual(quarantined["run"].run_digest, loaded_run.run_digest)
        self.assertEqual(
            quarantined["checkpoint"].checkpoint_digest,
            loaded_checkpoint.checkpoint_digest,
        )
        replay_authority = reloaded.open_model_reconciliation_authority(
            reservation_event_id=reserved["reservation_event_id"],
            lease_owner_id=self.fixture.owner,
            lease_token=self.fixture.token,
        )
        replay = reloaded.quarantine_model_reservation(
            reconciliation_authority=replay_authority,
            expected_run_digest=reserved["run"].run_digest,
            lease_owner_id=self.fixture.owner,
            lease_token=self.fixture.token,
        )
        self.assertTrue(replay["idempotent_replay"])

    def test_expired_authorization_cannot_append_normal_supervision(self) -> None:
        checkpoint = self.store.latest_checkpoint(self.run.run_id)
        event = create_supervision_event(
            run_id=self.run.run_id,
            sequence=1,
            event_type="heartbeat",
            severity="info",
            observed_at=T_SETTLED,
            detail="Observation created before authority expiry.",
            evidence={"checkpoint_digest": checkpoint.checkpoint_digest},
            action_taken=("checkpoint_observed",),
        )
        self.fixture.now = T_EXPIRED
        with self.assertRaises(OperationalStateError) as expired:
            self.store.record_supervision_event(
                event=event,
                expected_run_digest=self.run.run_digest,
                source_authority=self.fixture.source_authority,
                lease_owner_id=self.fixture.owner,
                lease_token=self.fixture.token,
            )
        self.assert_reason(expired, "authorization_expired_or_not_yet_valid")
        self.assertEqual((), self.store.list_supervision_events(self.run.run_id))

    def test_restart_preserves_pending_and_settled_budget_without_duplicate_call(self) -> None:
        reasoning = self.transition(self.run, "reasoning")
        reservation = self.reservation(suffix="restart")
        reserved = self.reserve(reasoning, self.authorization, reservation)
        restarted = CognitiveExecutionStore(
            self.fixture.database,
            trusted_clock=lambda: self.fixture.now,
        )
        pending_run = restarted.get_run(self.run.run_id)
        pending_checkpoint = restarted.latest_checkpoint(self.run.run_id)
        self.assertEqual(reserved["run"].run_digest, pending_run.run_digest)
        self.assertEqual(
            self.budget_state(reservation, settled=False),
            pending_checkpoint.to_dict()["model_budget_state"],
        )
        recovery = restarted.reserve_model_call(
            run_id=pending_run.run_id,
            expected_run_digest=pending_run.run_digest,
            authorization=self.authorization,
            source_authority=self.fixture.source_authority,
            provider_id=self.profile.provider_id,
            provider_profile_version=self.profile.profile_version,
            provider_profile_digest=self.profile.profile_digest,
            request_digest=reservation.request_digest,
            reservation=reservation,
            lease_owner_id=self.fixture.owner,
            lease_token=self.fixture.token,
        )
        self.assertEqual("recovery_required", recovery["status"])
        self.assertEqual("model_call_outcome_unknown", recovery["reason_code"])
        self.assertFalse(recovery["provider_call_allowed"])

        settled = self.settle(reserved, reservation, usage=self.usage())
        restarted_again = CognitiveExecutionStore(
            self.fixture.database,
            trusted_clock=lambda: self.fixture.now,
        )
        loaded = restarted_again.get_run(self.run.run_id)
        loaded_checkpoint = restarted_again.latest_checkpoint(self.run.run_id)
        self.assertEqual(settled["run"].run_digest, loaded.run_digest)
        self.assertEqual(
            settled["checkpoint"].to_dict()["model_budget_state"],
            loaded_checkpoint.to_dict()["model_budget_state"],
        )


if __name__ == "__main__":
    unittest.main()
