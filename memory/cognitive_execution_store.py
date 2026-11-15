from __future__ import annotations

import json
import hashlib
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from memory.cognitive_authorization import (
    CognitiveAuthorizationArtifact,
    SystemModelReconciliationAuthority,
    SystemReconciliationAuthority,
    _delegation_allows_capability,
    _authorization_from_verified_record,
    _issue_system_model_reconciliation_authority,
    _issue_system_reconciliation_authority,
)
from memory.cognitive_cancellation import (
    UserCancellationArtifact,
    _cancellation_from_verified_record,
)
from memory.cognitive_execution_models import (
    CognitiveRun,
    CognitiveRunCheckpoint,
    TOOL_PROTOCOL_VERSION,
    ToolExecutionPreview,
    ToolExecutionResult,
    ToolProposal,
    _create_cognitive_checkpoint,
    _create_cognitive_run,
    _tool_execution_from_verified_record,
    _tool_proposal_from_verified_record,
)
from memory.cognitive_orchestration import (
    ModelBudget,
    ModelBudgetState,
    ModelCallReservation,
    reserve_model_call as calculate_model_call_reservation,
    settle_model_call as calculate_model_call_settlement,
)
from memory.cognitive_provider import CognitiveProviderError, ProviderProfile
from memory.cognitive_state_migration import verify_cognitive_state_schema
from memory.cognitive_supervision import SupervisionEvent
from memory.cognitive_verification import (
    IndependentToolVerification,
    assert_independent_verification_store_binding,
)
from memory.operational_state_delegation import DelegationContract
from memory.operational_state_models import (
    AuthorityArtifact,
    OperationalStateError,
    SourceAnchor,
    build_state_event,
    canonical_digest,
    canonical_json,
    make_identity,
    normalize_timestamp,
    utc_now,
    validate_sha256,
)
from memory.operational_state_store import OperationalStateStore


def _elapsed_milliseconds(started_at: str, current_at: str) -> int:
    try:
        started = datetime.fromisoformat(
            normalize_timestamp(started_at).replace("Z", "+00:00")
        )
        current = datetime.fromisoformat(
            normalize_timestamp(current_at).replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        raise OperationalStateError("cognitive_run_time_boundary_invalid") from None
    elapsed = int((current - started).total_seconds() * 1000)
    if elapsed < 0:
        raise OperationalStateError("cognitive_run_time_boundary_invalid")
    return elapsed


EXECUTION_STORE_VERSION = 1
_MAX_CANONICAL_BYTES = 2 * 1024 * 1024

_RUN_TRANSITIONS = {
    "queued": {
        "reasoning",
        "planning",
        "waiting_authority",
        "executing",
        "failed",
        "cancelled",
        "recovery_required",
    },
    "reasoning": {
        "planning",
        "waiting_authority",
        "executing",
        "failed",
        "cancelled",
        "recovery_required",
    },
    "planning": {
        "waiting_authority",
        "executing",
        "failed",
        "cancelled",
        "recovery_required",
    },
    "waiting_authority": {
        "executing",
        "failed",
        "cancelled",
        "recovery_required",
    },
    "executing": {
        "waiting_external",
        "waiting_authority",
        "verifying",
        "failed",
        "cancelled",
        "recovery_required",
    },
    "waiting_external": {
        "executing",
        "waiting_authority",
        "failed",
        "cancelled",
        "recovery_required",
    },
    "verifying": {
        "waiting_acceptance",
        "failed",
        "cancelled",
        "recovery_required",
    },
    "waiting_acceptance": {"completed", "failed", "cancelled"},
    "recovery_required": {
        "executing",
        "waiting_authority",
        "failed",
        "cancelled",
    },
    "completed": set(),
    "failed": set(),
    "cancelled": set(),
}


def _decode_canonical(raw: Any, reason_code: str) -> dict[str, Any]:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > _MAX_CANONICAL_BYTES:
        raise OperationalStateError(reason_code)
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        raise OperationalStateError(reason_code) from None
    if not isinstance(value, dict) or canonical_json(value) != raw:
        raise OperationalStateError(reason_code)
    return value


_MODEL_BUDGET_STATE_FIELDS = {
    "calls_reserved",
    "input_tokens_reserved",
    "output_tokens_reserved",
    "cost_microunits_reserved",
    "request_digests",
    "settled_reservation_ids",
    "actual_input_tokens",
    "actual_output_tokens",
    "actual_cost_microunits",
    "unknown_cost_reserved_ceiling_microunits",
    "unknown_cost_reservation_ids",
}


def _model_budget_state_payload(
    state: ModelBudgetState,
) -> dict[str, Any]:
    if not isinstance(state, ModelBudgetState):
        raise OperationalStateError("typed_model_budget_state_required")
    return {
        "calls_reserved": state.calls_reserved,
        "input_tokens_reserved": state.input_tokens_reserved,
        "output_tokens_reserved": state.output_tokens_reserved,
        "cost_microunits_reserved": state.cost_microunits_reserved,
        "request_digests": list(state.request_digests),
        "settled_reservation_ids": list(state.settled_reservation_ids),
        "actual_input_tokens": state.actual_input_tokens,
        "actual_output_tokens": state.actual_output_tokens,
        "actual_cost_microunits": state.actual_cost_microunits,
        "unknown_cost_reserved_ceiling_microunits": (
            state.unknown_cost_reserved_ceiling_microunits
        ),
        "unknown_cost_reservation_ids": list(state.unknown_cost_reservation_ids),
    }


def _model_budget_state_from_payload(
    payload: Mapping[str, Any],
) -> ModelBudgetState:
    if not isinstance(payload, Mapping) or set(payload) != _MODEL_BUDGET_STATE_FIELDS:
        raise OperationalStateError("invalid_persisted_model_budget_state")
    values = dict(payload)
    for name in (
        "request_digests",
        "settled_reservation_ids",
        "unknown_cost_reservation_ids",
    ):
        if (
            isinstance(values.get(name), (str, bytes))
            or not isinstance(values.get(name), Sequence)
        ):
            raise OperationalStateError("invalid_persisted_model_budget_state")
        values[name] = tuple(values[name])
    try:
        state = ModelBudgetState(**values)
    except Exception:
        raise OperationalStateError(
            "invalid_persisted_model_budget_state"
        ) from None
    return state


def _empty_model_budget_state_payload() -> dict[str, Any]:
    return _model_budget_state_payload(ModelBudgetState())


def _model_call_reservation_payload(
    reservation: ModelCallReservation,
) -> dict[str, Any]:
    if not isinstance(reservation, ModelCallReservation):
        raise OperationalStateError("typed_model_call_reservation_required")
    return {
        "reservation_id": reservation.reservation_id,
        "request_digest": reservation.request_digest,
        "provider_binding": reservation.provider_binding.to_dict(),
        "input_tokens": reservation.input_tokens,
        "output_token_ceiling": reservation.output_token_ceiling,
        "cost_microunits_ceiling": reservation.cost_microunits_ceiling,
        "sequence": reservation.sequence,
        "grants_authority": reservation.grants_authority,
        "execution_allowed": reservation.execution_allowed,
        "status": "reserved",
        "runtime_execution_reference": None,
        "runtime_receipt_digest": None,
        "actual_input_tokens": None,
        "actual_output_tokens": None,
        "actual_cost_microunits": None,
        "settlement_digest": None,
    }


def _bounded_model_budget(
    *,
    authorization: CognitiveAuthorizationArtifact,
    state: ModelBudgetState,
    reservation: ModelCallReservation,
    explicit_budget: ModelBudget | None,
) -> ModelBudget:
    required_input = state.input_tokens_reserved + reservation.input_tokens
    required_output = (
        state.output_tokens_reserved + reservation.output_token_ceiling
    )
    if state.calls_reserved + 1 > authorization.max_model_calls:
        raise OperationalStateError("model_call_budget_exhausted")
    if required_input + required_output > authorization.max_tokens:
        raise OperationalStateError("model_token_budget_exhausted")
    if (
        state.cost_microunits_reserved
        + reservation.cost_microunits_ceiling
        > authorization.max_cost_microunits
    ):
        raise OperationalStateError("model_cost_budget_exhausted")
    if explicit_budget is not None:
        if not isinstance(explicit_budget, ModelBudget):
            raise OperationalStateError("typed_model_budget_required")
        if (
            explicit_budget.maximum_model_calls
            > authorization.max_model_calls
            or explicit_budget.maximum_total_tokens > authorization.max_tokens
            or explicit_budget.maximum_input_tokens > authorization.max_tokens
            or explicit_budget.maximum_output_tokens > authorization.max_tokens
            or explicit_budget.maximum_cost_microunits
            > authorization.max_cost_microunits
        ):
            raise OperationalStateError("model_budget_exceeds_authorization")
        return explicit_budget
    remaining = authorization.max_tokens - required_input - required_output
    return ModelBudget(
        maximum_model_calls=authorization.max_model_calls,
        maximum_input_tokens=required_input + remaining,
        maximum_output_tokens=required_output,
        maximum_total_tokens=authorization.max_tokens,
        maximum_cost_microunits=authorization.max_cost_microunits,
    )


def _settlement_model_budget(
    *,
    authorization: CognitiveAuthorizationArtifact,
    state: ModelBudgetState,
    explicit_budget: ModelBudget | None,
) -> ModelBudget:
    if explicit_budget is not None:
        if not isinstance(explicit_budget, ModelBudget):
            raise OperationalStateError("typed_model_budget_required")
        return explicit_budget
    if (
        state.input_tokens_reserved + state.output_tokens_reserved
        > authorization.max_tokens
        or state.cost_microunits_reserved
        > authorization.max_cost_microunits
        or state.calls_reserved > authorization.max_model_calls
    ):
        raise OperationalStateError("persisted_model_budget_exceeds_authorization")
    return ModelBudget(
        maximum_model_calls=authorization.max_model_calls,
        maximum_input_tokens=state.input_tokens_reserved,
        maximum_output_tokens=(
            authorization.max_tokens - state.input_tokens_reserved
        ),
        maximum_total_tokens=authorization.max_tokens,
        maximum_cost_microunits=authorization.max_cost_microunits,
    )


class CognitiveExecutionStore:
    """Execution facade over the existing Operational State database only."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        trusted_clock: Callable[[], str] | None = None,
        allow_production_write: bool = False,
    ) -> None:
        self.path = Path(path).expanduser().resolve()
        self.operational = OperationalStateStore(
            self.path,
            allow_production_write=allow_production_write,
        )
        self._trusted_clock = trusted_clock or utc_now
        verify_cognitive_state_schema(self.path)

    def _trusted_at(self) -> str:
        return normalize_timestamp(self._trusted_clock())

    @staticmethod
    def _assert_source_authority(
        authorization: CognitiveAuthorizationArtifact,
        source_authority: AuthorityArtifact,
    ) -> None:
        if (
            source_authority.authority_id != authorization.source_authority_id
            or source_authority.authority_digest
            != authorization.source_authority_digest
            or source_authority.issued_by != authorization.issuer_id
            or source_authority.issued_by.startswith("agent:")
        ):
            raise OperationalStateError("cognitive_authority_source_mismatch")

    @staticmethod
    def _resource_event(
        *,
        source_authority: AuthorityArtifact,
        project_id: str,
        entity_id: str,
        event_type: str,
        actor_kind: str,
        actor_id: str,
        at: str,
        expires_at: str | None,
        payload: Mapping[str, Any],
        idempotency_key: str,
        authority_kind: str,
        source_kind: str,
    ):
        evidence = {
            "entity_id": entity_id,
            "event_type": event_type,
            "payload": dict(payload),
            "idempotency_key": idempotency_key,
        }
        anchor = SourceAnchor(
            anchor_id=make_identity("anchor", evidence),
            source_kind=(
                "user_declaration" if actor_kind == "user" else "runtime"
            ),
            locator=f"cognitive-execution:{entity_id}",
            evidence_digest=canonical_digest(evidence),
            observed_at=at,
            privacy_scope="private_metadata",
        )
        return build_state_event(
            project_id=project_id,
            entity_id=entity_id,
            entity_type="resource",
            event_type=event_type,
            source_kind=source_kind,
            source_anchor=anchor,
            authority_kind=authority_kind,
            actor_kind=actor_kind,
            actor_id=actor_id,
            observed_at=at,
            effective_at=at,
            recorded_at=at,
            expires_at=expires_at,
            supersedes_event_id=None,
            caused_by_event_id=None,
            idempotency_key=idempotency_key,
            conflict_group_id=None,
            payload={"state": "available", **dict(payload)},
            privacy_scope="private_metadata",
            authority_digest=source_authority.authority_digest,
        )

    @staticmethod
    def _validate_persisted_bindings(
        connection: sqlite3.Connection,
        authorization: CognitiveAuthorizationArtifact,
    ) -> None:
        objective = connection.execute(
            "SELECT canonical_digest FROM objective_contracts "
            "WHERE objective_id=? AND version=?",
            (authorization.objective_id, authorization.objective_version),
        ).fetchone()
        plan = connection.execute(
            "SELECT plan_digest FROM plan_graphs WHERE plan_id=? AND version=?",
            (authorization.plan_id, authorization.plan_version),
        ).fetchone()
        delegation = connection.execute(
            "SELECT canonical_contract,contract_digest,authority_digest,project_id "
            "FROM delegation_contracts WHERE delegation_id=?",
            (authorization.delegation_id,),
        ).fetchone()
        if objective is None or objective[0] != authorization.objective_digest:
            raise OperationalStateError("authorization_objective_record_mismatch")
        if plan is None or plan[0] != authorization.plan_digest:
            raise OperationalStateError("authorization_plan_record_mismatch")
        if delegation is None:
            raise OperationalStateError("authorization_delegation_record_missing")
        contract_payload = _decode_canonical(
            delegation["canonical_contract"],
            "delegation_contract_record_corrupt",
        )
        contract = DelegationContract.from_dict(contract_payload)
        if (
            contract.contract_digest != delegation["contract_digest"]
            or contract.contract_digest != authorization.delegation_digest
            or contract.delegation_id != authorization.delegation_id
            or contract.project_id != authorization.project_id
            or delegation["project_id"] != authorization.project_id
            or contract.authority_digest != authorization.source_authority_digest
            or delegation["authority_digest"]
            != authorization.source_authority_digest
        ):
            raise OperationalStateError("authorization_delegation_record_mismatch")
        risk_order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
        if risk_order[authorization.risk_ceiling] > risk_order[contract.risk]:
            raise OperationalStateError("authorization_delegation_risk_expansion")
        for root in authorization.read_roots:
            if contract.validate_action(
                {"action": "read", "target": root}, at=authorization.valid_from
            ):
                raise OperationalStateError("authorization_delegation_read_expansion")
        for root in authorization.write_roots:
            if contract.validate_action(
                {"action": "write", "target": root}, at=authorization.valid_from
            ):
                raise OperationalStateError("authorization_delegation_write_expansion")
        for requirement in authorization.allowed_capabilities:
            if not _delegation_allows_capability(
                contract.tool_scope,
                requirement.capability_id,
            ):
                raise OperationalStateError("authorization_delegation_tool_expansion")

    def commit_authorization(
        self,
        *,
        authorization: CognitiveAuthorizationArtifact,
        source_authority: AuthorityArtifact,
        lease_owner_id: str,
        lease_token: str,
    ) -> dict[str, Any]:
        if not isinstance(authorization, CognitiveAuthorizationArtifact):
            raise OperationalStateError("invalid_cognitive_authorization")
        self._assert_source_authority(authorization, source_authority)
        trusted_at = self._trusted_at()
        authorization.assert_current(trusted_at)
        event_entity_id = make_identity(
            "authorizationrecord",
            {"authority_digest": authorization.authority_digest},
        )
        event = self._resource_event(
            source_authority=source_authority,
            project_id=authorization.project_id,
            entity_id=event_entity_id,
            event_type="authorization_artifact_issued",
            actor_kind="user",
            actor_id=source_authority.issued_by,
            at=trusted_at,
            expires_at=authorization.expires_at,
            payload={
                "authorization_id": authorization.authorization_id,
                "authority_digest": authorization.authority_digest,
                "delegation_id": authorization.delegation_id,
            },
            idempotency_key=make_identity(
                "idempotency",
                {
                    "operation": "authorization_artifact_issued",
                    "authority_digest": authorization.authority_digest,
                },
            ),
            authority_kind="user",
            source_kind="declared",
        )
        preview = self.operational.preview_event(
            authority=source_authority,
            event=event,
        )
        canonical = canonical_json(authorization.to_dict())
        with closing(self.operational._connect(write=True)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._validate_persisted_bindings(connection, authorization)
                existing = connection.execute(
                    "SELECT canonical_authorization,authority_digest,created_event_id "
                    "FROM authorization_artifacts WHERE authorization_id=?",
                    (authorization.authorization_id,),
                ).fetchone()
                if existing is not None:
                    if (
                        existing["canonical_authorization"] != canonical
                        or existing["authority_digest"]
                        != authorization.authority_digest
                        or existing["created_event_id"] != event.event_id
                    ):
                        raise OperationalStateError(
                            "authorization_identity_collision"
                        )
                    connection.rollback()
                    return {
                        "authorization_id": authorization.authorization_id,
                        "authority_digest": authorization.authority_digest,
                        "created_event_id": event.event_id,
                        "idempotent_replay": True,
                    }
                receipt = self.operational._commit_preview_on_connection(
                    connection,
                    authority=source_authority,
                    preview=preview,
                    lease_owner_id=lease_owner_id,
                    lease_token=lease_token,
                )
                connection.execute(
                    "INSERT INTO authorization_artifacts("
                    "authorization_id,objective_id,objective_version,objective_digest,"
                    "plan_id,plan_version,plan_digest,project_id,delegation_id,"
                    "delegation_digest,"
                    "source_authority_id,source_authority_digest,issuer_kind,issuer_id,"
                    "nonce_digest,risk_ceiling,max_steps,max_duration_ms,max_model_calls,"
                    "max_tokens,max_cost_microunits,commit_permission,merge_permission,"
                    "tag_permission,push_permission,network_permission,database_permission,"
                    "next_version_creation_permission,persistent_background_agent_permission,"
                    "valid_from,expires_at,canonical_authorization,authority_digest,"
                    "created_event_id,created_at"
                    ") VALUES(" + ",".join("?" for _ in range(35)) + ")",
                    (
                        authorization.authorization_id,
                        authorization.objective_id,
                        authorization.objective_version,
                        authorization.objective_digest,
                        authorization.plan_id,
                        authorization.plan_version,
                        authorization.plan_digest,
                        authorization.project_id,
                        authorization.delegation_id,
                        authorization.delegation_digest,
                        authorization.source_authority_id,
                        authorization.source_authority_digest,
                        authorization.issuer_kind,
                        authorization.issuer_id,
                        authorization.nonce_digest,
                        authorization.risk_ceiling,
                        authorization.max_steps,
                        authorization.max_duration_ms,
                        authorization.max_model_calls,
                        authorization.max_tokens,
                        authorization.max_cost_microunits,
                        int(authorization.commit_permission),
                        0,
                        0,
                        0,
                        0,
                        authorization.database_permission,
                        0,
                        0,
                        authorization.valid_from,
                        authorization.expires_at,
                        canonical,
                        authorization.authority_digest,
                        event.event_id,
                        trusted_at,
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {
            "authorization_id": authorization.authorization_id,
            "authority_digest": authorization.authority_digest,
            "created_event_id": event.event_id,
            "event_receipt_digest": receipt.receipt_digest,
            "idempotent_replay": False,
        }

    @staticmethod
    def _authorization_from_row(row: sqlite3.Row) -> CognitiveAuthorizationArtifact:
        payload = _decode_canonical(
            row["canonical_authorization"],
            "authorization_record_corrupt",
        )
        authorization = _authorization_from_verified_record(payload)
        if (
            authorization.authorization_id != row["authorization_id"]
            or authorization.authority_digest != row["authority_digest"]
            or authorization.objective_id != row["objective_id"]
            or authorization.plan_id != row["plan_id"]
            or authorization.delegation_id != row["delegation_id"]
            or authorization.delegation_digest != row["delegation_digest"]
            or authorization.source_authority_id != row["source_authority_id"]
        ):
            raise OperationalStateError("authorization_record_tampered")
        return authorization

    def get_authorization(self, authorization_id: str) -> CognitiveAuthorizationArtifact:
        with closing(self.operational._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM authorization_artifacts WHERE authorization_id=?",
                (authorization_id,),
            ).fetchone()
            if row is None:
                raise OperationalStateError("cognitive_authorization_not_found")
            authorization = self._authorization_from_row(row)
            self._validate_persisted_bindings(connection, authorization)
            event = connection.execute(
                "SELECT 1 FROM state_events WHERE event_id=?",
                (row["created_event_id"],),
            ).fetchone()
            if event is None:
                raise OperationalStateError("authorization_source_event_missing")
            return authorization

    @staticmethod
    def _provider_digest(
        connection: sqlite3.Connection,
        provider_id: str,
        profile_version: int,
    ) -> str:
        row = connection.execute(
            "SELECT profile_digest FROM provider_registry "
            "WHERE provider_id=? AND profile_version=?",
            (provider_id, profile_version),
        ).fetchone()
        if row is None:
            raise OperationalStateError("cognitive_run_provider_missing")
        return row["profile_digest"]

    @staticmethod
    def _insert_checkpoint(
        connection: sqlite3.Connection,
        checkpoint: CognitiveRunCheckpoint,
    ) -> None:
        connection.execute(
            "INSERT INTO cognitive_run_checkpoints("
            "run_id,checkpoint_sequence,safe_to_resume,authorization_digest,"
            "last_completed_execution_id,canonical_checkpoint,checkpoint_digest,recorded_at"
            ") VALUES(?,?,?,?,?,?,?,?)",
            (
                checkpoint.run_id,
                checkpoint.checkpoint_sequence,
                int(checkpoint.safe_to_resume),
                checkpoint.authorization_digest,
                checkpoint.last_completed_execution_id,
                canonical_json(checkpoint.to_dict()),
                checkpoint.checkpoint_digest,
                checkpoint.recorded_at,
            ),
        )

    @staticmethod
    def _insert_run(connection: sqlite3.Connection, run: CognitiveRun) -> None:
        connection.execute(
            "INSERT INTO cognitive_runs("
            "run_id,objective_id,objective_version,plan_id,plan_version,provider_id,"
            "provider_profile_version,authorization_id,status,checkpoint_sequence,"
            "last_safe_checkpoint_digest,model_calls_used,tokens_used,cost_used_microunits,"
            "steps_used,started_at,last_activity_at,ended_at,failure_code,"
            "current_projection_digest"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run.run_id,
                run.objective_id,
                run.objective_version,
                run.plan_id,
                run.plan_version,
                run.provider_id,
                run.provider_profile_version,
                run.authorization_id,
                run.status,
                run.checkpoint_sequence,
                run.last_safe_checkpoint,
                run.model_calls_used,
                run.tokens_used,
                run.cost_used_microunits,
                run.steps_used,
                run.started_at,
                run.last_activity_at,
                run.ended_at,
                run.failure_code,
                run.run_digest,
            ),
        )

    def create_run(
        self,
        *,
        authorization: CognitiveAuthorizationArtifact,
        source_authority: AuthorityArtifact,
        provider_id: str,
        provider_profile_version: int,
        run_nonce: str,
        lease_owner_id: str,
        lease_token: str,
    ) -> CognitiveRun:
        self._assert_source_authority(authorization, source_authority)
        trusted_at = self._trusted_at()
        authorization.assert_current(trusted_at)
        if not isinstance(run_nonce, str) or not 16 <= len(run_nonce) <= 1024:
            raise OperationalStateError("invalid_cognitive_run_nonce")
        with closing(self.operational._connect()) as read_connection:
            self._validate_persisted_bindings(read_connection, authorization)
            provider_digest = self._provider_digest(
                read_connection,
                provider_id,
                provider_profile_version,
            )
            completed_plan_nodes = tuple(
                row["node_id"]
                for row in read_connection.execute(
                    "SELECT node_id FROM plan_nodes "
                    "WHERE plan_id=? AND plan_version=? AND state='completed' "
                    "ORDER BY node_id",
                    (authorization.plan_id, authorization.plan_version),
                ).fetchall()
            )
        run_id = make_identity(
            "cognitiverun",
            {
                "authorization_digest": authorization.authority_digest,
                "provider_id": provider_id,
                "provider_profile_version": provider_profile_version,
                "provider_profile_digest": provider_digest,
                "run_nonce": run_nonce,
            },
        )
        initial_checkpoint = _create_cognitive_checkpoint(
            run_id=run_id,
            checkpoint_sequence=1,
            safe_to_resume=True,
            run_status="queued",
            authorization_digest=authorization.authority_digest,
            objective_digest=authorization.objective_digest,
            plan_digest=authorization.plan_digest,
            provider_profile_digest=provider_digest,
            current_node_ids=(),
            completed_node_ids=completed_plan_nodes,
            model_budget_state=_empty_model_budget_state_payload(),
            runtime_recovery_bindings=(),
            proposal_ids=(),
            execution_ids=(),
            idempotency_keys=(),
            last_completed_execution_id=None,
            unsafe_reason_codes=(),
            steps_used=0,
            started_at=trusted_at,
            last_activity_at=trusted_at,
            ended_at=None,
            failure_code=None,
            recorded_at=trusted_at,
        )
        run = _create_cognitive_run(
            run_id=run_id,
            objective_id=authorization.objective_id,
            objective_version=authorization.objective_version,
            objective_digest=authorization.objective_digest,
            plan_id=authorization.plan_id,
            plan_version=authorization.plan_version,
            plan_digest=authorization.plan_digest,
            provider_id=provider_id,
            provider_profile_version=provider_profile_version,
            provider_profile_digest=provider_digest,
            authorization_id=authorization.authorization_id,
            authorization_digest=authorization.authority_digest,
            status="queued",
            checkpoint_sequence=1,
            last_safe_checkpoint=initial_checkpoint.checkpoint_digest,
            model_calls_used=0,
            tokens_used=0,
            cost_used_microunits=0,
            steps_used=0,
            started_at=trusted_at,
            last_activity_at=trusted_at,
            ended_at=None,
            failure_code=None,
        )
        event = self._resource_event(
            source_authority=source_authority,
            project_id=authorization.project_id,
            entity_id=run_id,
            event_type="cognitive_run_created",
            actor_kind="runtime",
            actor_id="runtime:yunos-cognitive-loop",
            at=trusted_at,
            expires_at=authorization.expires_at,
            payload={
                "run_id": run_id,
                "run_digest": run.run_digest,
                "authorization_digest": authorization.authority_digest,
            },
            idempotency_key=make_identity(
                "idempotency",
                {"operation": "cognitive_run_created", "run_id": run_id},
            ),
            authority_kind="verified_runtime",
            source_kind="observed",
        )
        preview = self.operational.preview_event(
            authority=source_authority,
            event=event,
        )
        with closing(self.operational._connect(write=True)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT * FROM cognitive_runs WHERE run_id=?", (run_id,)
                ).fetchone()
                if existing is not None:
                    loaded = self._run_from_row(connection, existing)
                    if loaded.run_digest != run.run_digest:
                        raise OperationalStateError("cognitive_run_identity_collision")
                    connection.rollback()
                    return loaded
                prior_authorization_run = connection.execute(
                    "SELECT run_id FROM cognitive_runs WHERE authorization_id=?",
                    (authorization.authorization_id,),
                ).fetchone()
                if prior_authorization_run is not None:
                    raise OperationalStateError(
                        "cognitive_authorization_already_bound_to_run"
                    )
                self.operational._commit_preview_on_connection(
                    connection,
                    authority=source_authority,
                    preview=preview,
                    lease_owner_id=lease_owner_id,
                    lease_token=lease_token,
                )
                self._insert_run(connection, run)
                self._insert_checkpoint(connection, initial_checkpoint)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return run

    def _run_from_row(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> CognitiveRun:
        objective = connection.execute(
            "SELECT canonical_digest FROM objective_contracts "
            "WHERE objective_id=? AND version=?",
            (row["objective_id"], row["objective_version"]),
        ).fetchone()
        plan = connection.execute(
            "SELECT plan_digest FROM plan_graphs WHERE plan_id=? AND version=?",
            (row["plan_id"], row["plan_version"]),
        ).fetchone()
        if objective is None or plan is None:
            raise OperationalStateError("cognitive_run_binding_missing")
        provider_digest = self._provider_digest(
            connection,
            row["provider_id"],
            row["provider_profile_version"],
        )
        authorization_digest = None
        if row["authorization_id"] is not None:
            auth = connection.execute(
                "SELECT authority_digest FROM authorization_artifacts "
                "WHERE authorization_id=?",
                (row["authorization_id"],),
            ).fetchone()
            if auth is None:
                raise OperationalStateError("cognitive_run_authorization_missing")
            authorization_digest = auth["authority_digest"]
        run = _create_cognitive_run(
            run_id=row["run_id"],
            objective_id=row["objective_id"],
            objective_version=row["objective_version"],
            objective_digest=objective["canonical_digest"],
            plan_id=row["plan_id"],
            plan_version=row["plan_version"],
            plan_digest=plan["plan_digest"],
            provider_id=row["provider_id"],
            provider_profile_version=row["provider_profile_version"],
            provider_profile_digest=provider_digest,
            authorization_id=row["authorization_id"],
            authorization_digest=authorization_digest,
            status=row["status"],
            checkpoint_sequence=row["checkpoint_sequence"],
            last_safe_checkpoint=row["last_safe_checkpoint_digest"],
            model_calls_used=row["model_calls_used"],
            tokens_used=row["tokens_used"],
            cost_used_microunits=row["cost_used_microunits"],
            steps_used=row["steps_used"],
            started_at=row["started_at"],
            last_activity_at=row["last_activity_at"],
            ended_at=row["ended_at"],
            failure_code=row["failure_code"],
        )
        if run.run_digest != row["current_projection_digest"]:
            raise OperationalStateError("cognitive_run_projection_tampered")
        return run

    def get_run(self, run_id: str) -> CognitiveRun:
        with closing(self.operational._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM cognitive_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                raise OperationalStateError("cognitive_run_not_found")
            return self._run_from_row(connection, row)

    @staticmethod
    def _update_run_row(
        connection: sqlite3.Connection,
        *,
        previous: CognitiveRun,
        current: CognitiveRun,
    ) -> None:
        cursor = connection.execute(
            "UPDATE cognitive_runs SET status=?,checkpoint_sequence=?,"
            "last_safe_checkpoint_digest=?,model_calls_used=?,tokens_used=?,"
            "cost_used_microunits=?,steps_used=?,started_at=?,last_activity_at=?,"
            "ended_at=?,failure_code=?,current_projection_digest=? "
            "WHERE run_id=? AND current_projection_digest=?",
            (
                current.status,
                current.checkpoint_sequence,
                current.last_safe_checkpoint,
                current.model_calls_used,
                current.tokens_used,
                current.cost_used_microunits,
                current.steps_used,
                current.started_at,
                current.last_activity_at,
                current.ended_at,
                current.failure_code,
                current.run_digest,
                current.run_id,
                previous.run_digest,
            ),
        )
        if cursor.rowcount != 1:
            raise OperationalStateError("cognitive_run_projection_conflict")

    @staticmethod
    def _checkpoint_inventory(
        connection: sqlite3.Connection,
        run: CognitiveRun,
    ) -> dict[str, tuple[str, ...]]:
        proposals = tuple(
            row[0]
            for row in connection.execute(
                "SELECT proposal_id FROM tool_proposals WHERE run_id=? "
                "ORDER BY proposal_id",
                (run.run_id,),
            ).fetchall()
        )
        executions = connection.execute(
            "SELECT execution_id,idempotency_key FROM tool_executions "
            "WHERE run_id=? ORDER BY execution_id",
            (run.run_id,),
        ).fetchall()
        return {
            "proposal_ids": proposals,
            "execution_ids": tuple(row["execution_id"] for row in executions),
            "idempotency_keys": tuple(
                row["idempotency_key"] for row in executions
            ),
        }

    @staticmethod
    def _checkpoint_from_row(row: sqlite3.Row) -> CognitiveRunCheckpoint:
        payload = _decode_canonical(
            row["canonical_checkpoint"],
            "cognitive_checkpoint_record_corrupt",
        )
        if payload.pop("protocol_version", None) != TOOL_PROTOCOL_VERSION:
            raise OperationalStateError(
                "cognitive_checkpoint_protocol_mismatch"
            )
        checkpoint = _create_cognitive_checkpoint(**payload)
        if (
            checkpoint.checkpoint_digest != row["checkpoint_digest"]
            or checkpoint.run_id != row["run_id"]
            or checkpoint.checkpoint_sequence != row["checkpoint_sequence"]
            or int(checkpoint.safe_to_resume) != row["safe_to_resume"]
            or checkpoint.authorization_digest != row["authorization_digest"]
            or checkpoint.last_completed_execution_id
            != row["last_completed_execution_id"]
            or checkpoint.recorded_at != row["recorded_at"]
        ):
            raise OperationalStateError("cognitive_checkpoint_record_tampered")
        return checkpoint

    @classmethod
    def _latest_checkpoint_on_connection(
        cls,
        connection: sqlite3.Connection,
        run_id: str,
    ) -> CognitiveRunCheckpoint:
        row = connection.execute(
            "SELECT * FROM "
            "cognitive_run_checkpoints WHERE run_id=? "
            "ORDER BY checkpoint_sequence DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        if row is None:
            raise OperationalStateError("cognitive_checkpoint_not_found")
        return cls._checkpoint_from_row(row)

    @classmethod
    def _assert_checkpoint_matches_run(
        cls,
        checkpoint: CognitiveRunCheckpoint,
        run: CognitiveRun,
    ) -> None:
        budget_state = _model_budget_state_from_payload(
            checkpoint.model_budget_state
        )
        if (
            checkpoint.run_id != run.run_id
            or checkpoint.checkpoint_sequence != run.checkpoint_sequence
            or checkpoint.run_status != run.status
            or checkpoint.authorization_digest != run.authorization_digest
            or checkpoint.objective_digest != run.objective_digest
            or checkpoint.plan_digest != run.plan_digest
            or checkpoint.provider_profile_digest
            != run.provider_profile_digest
            or checkpoint.steps_used != run.steps_used
            or checkpoint.started_at != run.started_at
            or checkpoint.last_activity_at != run.last_activity_at
            or checkpoint.ended_at != run.ended_at
            or checkpoint.failure_code != run.failure_code
            or budget_state.calls_reserved != run.model_calls_used
            or budget_state.actual_input_tokens
            + budget_state.actual_output_tokens
            != run.tokens_used
            or budget_state.actual_cost_microunits
            + budget_state.unknown_cost_reserved_ceiling_microunits
            != run.cost_used_microunits
        ):
            raise OperationalStateError(
                "cognitive_checkpoint_run_projection_mismatch"
            )

    @staticmethod
    def _unresolved_reservations_on_connection(
        connection: sqlite3.Connection,
        *,
        run: CognitiveRun,
        project_id: str,
    ) -> tuple[dict[str, Any], ...]:
        rows = connection.execute(
            "SELECT e.event_id,e.project_id,e.idempotency_key,e.canonical_payload,"
            "r.receipt_digest FROM state_events e "
            "JOIN state_receipts r ON r.event_id=e.event_id "
            "LEFT JOIN tool_executions t "
            "ON t.reservation_event_id=e.event_id "
            "AND t.idempotency_key=e.idempotency_key "
            "WHERE e.event_type='tool_execution_reserved' "
            "AND json_extract(e.canonical_payload,'$.run_id')=? "
            "AND t.execution_id IS NULL ORDER BY e.recorded_at,e.event_id",
            (run.run_id,),
        ).fetchall()
        unresolved: list[dict[str, Any]] = []
        for row in rows:
            payload = _decode_canonical(
                row["canonical_payload"],
                "tool_reservation_record_corrupt",
            )
            sealed_preview = payload.get("sealed_preview")
            if (
                row["project_id"] != project_id
                or payload.get("state") != "available"
                or payload.get("run_id") != run.run_id
                or payload.get("authorization_digest")
                != run.authorization_digest
                or payload.get("execution_key") != row["idempotency_key"]
                or payload.get("operation_executed") is not False
                or not isinstance(sealed_preview, dict)
                or sealed_preview.get("preview_id")
                != payload.get("preview_id")
                or sealed_preview.get("preview_digest")
                != payload.get("preview_digest")
                or canonical_digest(
                    {
                        key: value
                        for key, value in sealed_preview.items()
                        if key != "preview_digest"
                    }
                )
                != payload.get("preview_digest")
            ):
                raise OperationalStateError(
                    "tool_reservation_record_binding_mismatch"
                )
            unresolved.append(
                {
                    "event_id": row["event_id"],
                    "receipt_digest": row["receipt_digest"],
                    "execution_key": row["idempotency_key"],
                    "payload": payload,
                }
            )
        return tuple(unresolved)

    @staticmethod
    def _assert_tool_verifications_complete(
        connection: sqlite3.Connection,
        *,
        run_id: str,
    ) -> None:
        missing = connection.execute(
            "SELECT 1 FROM tool_executions t "
            "WHERE t.run_id=? AND t.status='succeeded' AND NOT EXISTS ("
            "SELECT 1 FROM state_verifications v "
            "WHERE json_extract(v.canonical_verification,"
            "'$.details.tool_execution_id')=t.execution_id "
            "AND json_extract(v.canonical_verification,"
            "'$.details.execution_digest')=t.execution_digest "
            "AND v.result='passed') LIMIT 1",
            (run_id,),
        ).fetchone()
        if missing is not None:
            raise OperationalStateError(
                "cognitive_run_independent_verification_incomplete"
            )

    @staticmethod
    def _bounded_external_pause_is_safe(
        connection: sqlite3.Connection,
        *,
        run: CognitiveRun,
        checkpoint: CognitiveRunCheckpoint,
    ) -> bool:
        """Recognize only a durable retry/replan backoff with no owned change."""

        if (
            run.status != "executing"
            or checkpoint.run_status != "executing"
            or not checkpoint.safe_to_resume
            or checkpoint.unsafe_reason_codes
            or not checkpoint.current_node_ids
        ):
            return False
        supervision_rows = connection.execute(
            "SELECT canonical_payload,recorded_at FROM state_events "
            "WHERE entity_id=? AND event_type='cognitive_run_update' "
            "ORDER BY recorded_at,event_id",
            (run.run_id,),
        ).fetchall()
        supervision_events: list[tuple[SupervisionEvent, str]] = []
        for row in supervision_rows:
            payload = _decode_canonical(
                row["canonical_payload"],
                "cognitive_supervision_record_corrupt",
            )
            if payload.get("operation") != "supervision_record":
                continue
            raw_event = payload.get("supervision_event")
            if not isinstance(raw_event, Mapping):
                raise OperationalStateError(
                    "cognitive_supervision_record_corrupt"
                )
            try:
                event = SupervisionEvent.from_dict(raw_event)
            except (TypeError, ValueError, OperationalStateError):
                raise OperationalStateError(
                    "cognitive_supervision_record_corrupt"
                ) from None
            if event.run_id != run.run_id or event.observed_at != row["recorded_at"]:
                raise OperationalStateError(
                    "cognitive_supervision_record_corrupt"
                )
            supervision_events.append((event, row["recorded_at"]))
        if tuple(item[0].sequence for item in supervision_events) != tuple(
            range(1, len(supervision_events) + 1)
        ):
            raise OperationalStateError(
                "cognitive_supervision_sequence_corrupt"
            )
        if not supervision_events:
            return False
        event, _recorded_at = supervision_events[-1]
        expected_actions = {
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
        if (
            event.event_type != "retry_started"
            or event.detail not in expected_actions
            or event.severity != "warning"
            or event.action_taken != expected_actions[event.detail]
            or event.plan_node_id is None
            or event.tool_execution_id is None
            or event.plan_node_id not in checkpoint.current_node_ids
            or event.tool_execution_id not in checkpoint.execution_ids
        ):
            return False

        execution_row = connection.execute(
            "SELECT * FROM tool_executions WHERE execution_id=? AND run_id=?",
            (event.tool_execution_id, run.run_id),
        ).fetchone()
        latest_execution = connection.execute(
            "SELECT execution_id FROM tool_executions WHERE run_id=? "
            "ORDER BY recorded_at DESC,execution_id DESC LIMIT 1",
            (run.run_id,),
        ).fetchone()
        if (
            execution_row is None
            or latest_execution is None
            or latest_execution["execution_id"] != event.tool_execution_id
        ):
            return False
        execution = CognitiveExecutionStore._execution_from_row(execution_row)
        if (
            execution.status != "failed"
            or execution.verification_status != "failed"
            or "tool_execution_no_owned_change" not in execution.reason_codes
        ):
            return False
        proposal_row = connection.execute(
            "SELECT * FROM tool_proposals WHERE proposal_id=?",
            (execution.proposal_id,),
        ).fetchone()
        if proposal_row is None:
            raise OperationalStateError("tool_verification_proposal_missing")
        proposal = CognitiveExecutionStore._proposal_from_row(proposal_row)
        if (
            proposal.run_id != run.run_id
            or proposal.plan_node_id != event.plan_node_id
            or proposal.proposal_digest != execution.proposal_digest
            or proposal.authorization_digest != run.authorization_digest
        ):
            return False

        verification_rows = connection.execute(
            "SELECT * FROM state_verifications WHERE "
            "json_extract(canonical_verification,"
            "'$.details.tool_execution_id')=? "
            "ORDER BY recorded_at,verification_id",
            (execution.execution_id,),
        ).fetchall()
        if len(verification_rows) != 1:
            return False
        verification_row = verification_rows[0]
        verification = _decode_canonical(
            verification_row["canonical_verification"],
            "tool_verification_record_corrupt",
        )
        details = verification.get("details")
        independent = (
            details.get("independent_verification")
            if isinstance(details, Mapping)
            else None
        )
        record = (
            independent.get("verification_record")
            if isinstance(independent, Mapping)
            else None
        )
        execution_bindings = (
            independent.get("execution_bindings")
            if isinstance(independent, Mapping)
            else None
        )
        sealed_evidence = (
            independent.get("sealed_evidence")
            if isinstance(independent, Mapping)
            else None
        )
        if (
            canonical_digest(verification)
            != verification_row["verification_digest"]
            or verification.get("verification_id")
            != verification_row["verification_id"]
            or verification.get("entity_id") != verification_row["entity_id"]
            or verification.get("event_id") != verification_row["event_id"]
            or verification.get("verifier_kind")
            != verification_row["verifier_kind"]
            or verification.get("result") != verification_row["result"]
            or verification.get("evidence_digest")
            != verification_row["evidence_digest"]
            or verification.get("recorded_at")
            != verification_row["recorded_at"]
            or verification_row["result"] != "failed"
            or not isinstance(details, Mapping)
            or details.get("run_id") != run.run_id
            or details.get("tool_execution_id") != execution.execution_id
            or details.get("execution_digest") != execution.execution_digest
            or details.get("proposal_id") != proposal.proposal_id
            or details.get("proposal_digest") != proposal.proposal_digest
            or details.get("preview_id") != execution.preview_id
            or details.get("preview_digest") != execution.preview_digest
            or details.get("owned_change_state") != "none"
            or not isinstance(record, Mapping)
            or record.get("run_id") != run.run_id
            or record.get("objective_id") != run.objective_id
            or record.get("plan_node_id") != event.plan_node_id
            or record.get("tool_execution_id") != execution.execution_id
            or record.get("verifier_kind") != "independent_tool"
            or record.get("result") != "failed"
            or record.get("acceptance_state") != "not_requested"
            or record.get("acceptance_event_id") is not None
            or record.get("evidence_digest")
            != verification_row["evidence_digest"]
            or not isinstance(execution_bindings, Mapping)
            or execution_bindings.get("proposal_digest")
            != proposal.proposal_digest
            or execution_bindings.get("execution_digest")
            != execution.execution_digest
            or execution_bindings.get("preview_digest")
            != execution.preview_digest
            or independent.get("binding_digest")
            != canonical_digest(dict(execution_bindings))
            or not isinstance(sealed_evidence, Mapping)
            or canonical_digest(dict(sealed_evidence))
            != verification_row["evidence_digest"]
            or event.observed_at < verification_row["recorded_at"]
        ):
            return False
        return True

    def transition_run(
        self,
        *,
        run_id: str,
        expected_run_digest: str,
        next_status: str,
        source_authority: AuthorityArtifact,
        lease_owner_id: str,
        lease_token: str,
        failure_code: str | None = None,
        acceptance_event_id: str | None = None,
    ) -> CognitiveRun:
        trusted_at = self._trusted_at()
        with closing(self.operational._connect(write=True)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM cognitive_runs WHERE run_id=?", (run_id,)
                ).fetchone()
                if row is None:
                    raise OperationalStateError("cognitive_run_not_found")
                previous = self._run_from_row(connection, row)
                if previous.run_digest != expected_run_digest:
                    raise OperationalStateError("cognitive_run_projection_conflict")
                authorization: CognitiveAuthorizationArtifact | None = None
                if previous.authorization_id is not None:
                    authorization_row = connection.execute(
                        "SELECT * FROM authorization_artifacts "
                        "WHERE authorization_id=?",
                        (previous.authorization_id,),
                    ).fetchone()
                    if authorization_row is None:
                        raise OperationalStateError(
                            "cognitive_run_authorization_missing"
                        )
                    authorization = self._authorization_from_row(
                        authorization_row
                    )
                    self._validate_persisted_bindings(connection, authorization)
                    self._assert_source_authority(
                        authorization,
                        source_authority,
                    )
                if next_status == "executing":
                    if authorization is None:
                        raise OperationalStateError(
                            "cognitive_run_execution_requires_authorization"
                        )
                    authorization.assert_current(trusted_at)
                latest = self._latest_checkpoint_on_connection(
                    connection,
                    previous.run_id,
                )
                self._assert_checkpoint_matches_run(latest, previous)
                cancellation_latched = connection.execute(
                    "SELECT 1 FROM state_events WHERE entity_id=? "
                    "AND event_type='cognitive_run_update' "
                    "AND json_extract(canonical_payload,'$.operation')="
                    "'cancellation_latched' LIMIT 1",
                    (previous.run_id,),
                ).fetchone()
                if cancellation_latched is not None and next_status not in {
                    "cancelled",
                    "recovery_required",
                }:
                    raise OperationalStateError(
                        "cancellation_latch_blocks_transition"
                    )
                objective_project = connection.execute(
                    "SELECT project_id FROM objective_contracts "
                    "WHERE objective_id=? AND version=?",
                    (previous.objective_id, previous.objective_version),
                ).fetchone()
                if objective_project is None:
                    raise OperationalStateError(
                        "cognitive_run_objective_missing"
                    )
                unresolved = self._unresolved_reservations_on_connection(
                    connection,
                    run=previous,
                    project_id=objective_project["project_id"],
                )
                if unresolved:
                    if next_status in {"failed", "cancelled"}:
                        failure_code = failure_code or f"requested_{next_status}"
                        next_status = "recovery_required"
                    elif next_status != "recovery_required":
                        raise OperationalStateError(
                            "cognitive_run_unresolved_execution_reservation"
                        )
                budget_state = _model_budget_state_from_payload(
                    latest.model_budget_state
                )
                model_reservation_pending = (
                    budget_state.calls_reserved
                    != len(budget_state.settled_reservation_ids)
                )
                if model_reservation_pending:
                    if next_status in {"failed", "cancelled"}:
                        failure_code = failure_code or f"requested_{next_status}"
                        next_status = "recovery_required"
                    elif next_status != "recovery_required":
                        raise OperationalStateError(
                            "cognitive_run_unresolved_model_reservation"
                        )
                if next_status not in _RUN_TRANSITIONS.get(previous.status, set()):
                    raise OperationalStateError("invalid_cognitive_run_transition")
                if next_status in {
                    "executing",
                    "verifying",
                    "waiting_acceptance",
                    "completed",
                    "failed",
                    "cancelled",
                } and (
                    not latest.safe_to_resume or latest.unsafe_reason_codes
                ):
                    raise OperationalStateError(
                        "cognitive_run_checkpoint_not_safe"
                    )
                if next_status in {
                    "verifying",
                    "waiting_acceptance",
                    "completed",
                }:
                    if latest.current_node_ids:
                        raise OperationalStateError(
                            "cognitive_run_plan_nodes_incomplete"
                        )
                    required_nodes = {
                        item["node_id"]
                        for item in connection.execute(
                            "SELECT node_id FROM plan_nodes "
                            "WHERE plan_id=? AND plan_version=? "
                            "AND state<>'omitted'",
                            (previous.plan_id, previous.plan_version),
                        ).fetchall()
                    }
                    if not required_nodes.issubset(
                        set(latest.completed_node_ids)
                    ):
                        raise OperationalStateError(
                            "cognitive_run_plan_nodes_incomplete"
                        )
                    self._assert_tool_verifications_complete(
                        connection,
                        run_id=run_id,
                    )
                bounded_external_pause = (
                    next_status == "waiting_external"
                    and self._bounded_external_pause_is_safe(
                        connection,
                        run=previous,
                        checkpoint=latest,
                    )
                )
                if next_status == "completed":
                    if acceptance_event_id is None:
                        raise OperationalStateError(
                            "cognitive_run_completion_requires_acceptance"
                        )
                    event = connection.execute(
                        "SELECT e.event_type,e.actor_kind,e.project_id,e.entity_type,"
                        "e.entity_id,e.canonical_payload,o.delegation_id,"
                        "o.runtime_run_id,o.verification_digest,o.status "
                        "FROM state_events e JOIN outcome_records o "
                        "ON o.outcome_id=e.entity_id WHERE e.event_id=?",
                        (acceptance_event_id,),
                    ).fetchone()
                    payload = (
                        None
                        if event is None
                        else _decode_canonical(
                            event["canonical_payload"],
                            "cognitive_run_acceptance_event_corrupt",
                        )
                    )
                    if (
                        event is None
                        or event["event_type"] != "outcome_accepted"
                        or event["actor_kind"] != "user"
                        or event["entity_type"] != "outcome"
                        or event["project_id"]
                        != objective_project["project_id"]
                        or authorization is None
                        or event["delegation_id"]
                        != authorization.delegation_id
                        or event["runtime_run_id"] != previous.run_id
                        or event["verification_digest"] is None
                        or event["status"] != "accepted"
                        or payload.get("accepted") is not True
                        or payload.get("outcome_id") != event["entity_id"]
                        or payload.get("delegation_id")
                        != authorization.delegation_id
                        or payload.get("verification_digest")
                        != event["verification_digest"]
                    ):
                        raise OperationalStateError(
                            "cognitive_run_acceptance_event_invalid"
                        )
                self.operational._authorize_auxiliary_write(
                    connection,
                    authority=source_authority,
                    lease_owner_id=lease_owner_id,
                    lease_token=lease_token,
                    project_id=source_authority.target_project_id,
                    entity_id=run_id,
                    operation="cognitive_run_update",
                    at=trusted_at,
                    enforce_for_test=True,
                )
                ended_at = (
                    trusted_at
                    if next_status in {"completed", "failed", "cancelled"}
                    else None
                )
                failure = (
                    failure_code or previous.failure_code
                    if next_status in {"failed", "recovery_required"}
                    else None
                )
                safe = next_status != "recovery_required" and (
                    next_status != "waiting_external" or bounded_external_pause
                )
                checkpoint = _create_cognitive_checkpoint(
                    run_id=previous.run_id,
                    checkpoint_sequence=previous.checkpoint_sequence + 1,
                    safe_to_resume=safe,
                    run_status=next_status,
                    authorization_digest=previous.authorization_digest,
                    objective_digest=previous.objective_digest,
                    plan_digest=previous.plan_digest,
                    provider_profile_digest=previous.provider_profile_digest,
                    current_node_ids=latest.current_node_ids,
                    completed_node_ids=latest.completed_node_ids,
                    model_budget_state=dict(latest.model_budget_state),
                    runtime_recovery_bindings=latest.runtime_recovery_bindings,
                    proposal_ids=latest.proposal_ids,
                    execution_ids=latest.execution_ids,
                    idempotency_keys=latest.idempotency_keys,
                    last_completed_execution_id=(
                        latest.last_completed_execution_id if safe else None
                    ),
                    unsafe_reason_codes=(
                        ()
                        if safe
                        else latest.unsafe_reason_codes
                        or (f"cognitive_run_{next_status}",)
                    ),
                    steps_used=previous.steps_used,
                    started_at=previous.started_at,
                    last_activity_at=trusted_at,
                    ended_at=ended_at,
                    failure_code=failure,
                    recorded_at=trusted_at,
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
                    status=next_status,
                    checkpoint_sequence=checkpoint.checkpoint_sequence,
                    last_safe_checkpoint=(
                        checkpoint.checkpoint_digest
                        if safe
                        else previous.last_safe_checkpoint
                    ),
                    model_calls_used=previous.model_calls_used,
                    tokens_used=previous.tokens_used,
                    cost_used_microunits=previous.cost_used_microunits,
                    steps_used=previous.steps_used,
                    started_at=previous.started_at,
                    last_activity_at=trusted_at,
                    ended_at=ended_at,
                    failure_code=failure,
                )
                self._insert_checkpoint(connection, checkpoint)
                self._update_run_row(
                    connection,
                    previous=previous,
                    current=current,
                )
                connection.commit()
                return current
            except Exception:
                connection.rollback()
                raise

    def _model_reservation_binding_on_connection(
        self,
        connection: sqlite3.Connection,
        reservation_event_id: str,
    ) -> dict[str, Any]:
        raw_event = connection.execute(
            "SELECT event_id,project_id,entity_id,event_type,authority_digest,"
            "idempotency_key,recorded_at FROM state_events WHERE event_id=?",
            (reservation_event_id,),
        ).fetchone()
        receipt_row = connection.execute(
            "SELECT * FROM state_receipts WHERE event_id=?",
            (reservation_event_id,),
        ).fetchone()
        if raw_event is None or receipt_row is None:
            raise OperationalStateError(
                "model_reconciliation_reservation_receipt_missing"
            )
        event = self.operational._load_event(connection, reservation_event_id)
        receipt = self.operational._receipt_from_row(
            receipt_row,
            idempotent_replay=True,
        )
        payload = dict(event.canonical_payload)
        expected_payload_fields = {
            "state",
            "run_id",
            "authorization_digest",
            "provider_id",
            "provider_profile_version",
            "provider_profile_digest",
            "request_digest",
            "reservation_id",
            "reservation_digest",
            "reservation",
            "operation_executed",
        }
        reservation_record = payload.get("reservation")
        expected_reservation_fields = {
            "reservation_id",
            "request_digest",
            "provider_binding",
            "input_tokens",
            "output_token_ceiling",
            "cost_microunits_ceiling",
            "sequence",
            "grants_authority",
            "execution_allowed",
            "status",
            "runtime_execution_reference",
            "runtime_receipt_digest",
            "actual_input_tokens",
            "actual_output_tokens",
            "actual_cost_microunits",
            "settlement_digest",
        }
        if (
            raw_event["event_type"] != "model_call_reserved"
            or set(payload) != expected_payload_fields
            or payload.get("state") != "available"
            or payload.get("operation_executed") is not False
            or not isinstance(reservation_record, dict)
            or set(reservation_record) != expected_reservation_fields
            or payload.get("reservation_id") != raw_event["idempotency_key"]
            or reservation_record.get("reservation_id")
            != payload.get("reservation_id")
            or reservation_record.get("request_digest")
            != payload.get("request_digest")
            or canonical_digest(reservation_record)
            != payload.get("reservation_digest")
            or raw_event["entity_id"]
            != make_identity(
                "modelreservation",
                {
                    "run_id": payload.get("run_id"),
                    "reservation_id": payload.get("reservation_id"),
                },
            )
            or receipt.event_id != reservation_event_id
            or receipt.idempotency_key != raw_event["idempotency_key"]
            or receipt.authority_digest != raw_event["authority_digest"]
            or receipt.recorded_at != raw_event["recorded_at"]
        ):
            raise OperationalStateError(
                "model_reconciliation_reservation_binding_mismatch"
            )
        for value, code in (
            (payload.get("authorization_digest"), "invalid_model_authorization_digest"),
            (payload.get("provider_profile_digest"), "invalid_provider_profile_digest"),
            (payload.get("request_digest"), "invalid_model_request_digest"),
            (payload.get("reservation_digest"), "invalid_model_reservation_digest"),
        ):
            validate_sha256(value, code)
        for field_name in (
            "input_tokens",
            "output_token_ceiling",
            "cost_microunits_ceiling",
            "sequence",
        ):
            value = reservation_record.get(field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise OperationalStateError(
                    "model_reconciliation_reservation_binding_mismatch"
                )
        if (
            reservation_record["sequence"] < 1
            or reservation_record.get("grants_authority") is not False
            or reservation_record.get("execution_allowed") is not False
            or reservation_record.get("status") != "reserved"
            or any(
                reservation_record.get(name) is not None
                for name in (
                    "runtime_execution_reference",
                    "runtime_receipt_digest",
                    "actual_input_tokens",
                    "actual_output_tokens",
                    "actual_cost_microunits",
                    "settlement_digest",
                )
            )
        ):
            raise OperationalStateError(
                "model_reconciliation_reservation_binding_mismatch"
            )
        provider_binding = reservation_record.get("provider_binding")
        provider_binding_fields = {
            "provider_id",
            "profile_version",
            "profile_digest",
            "model",
            "selection_mode",
            "selection_digest",
            "health_attestation_digest",
            "binding_digest",
        }
        if (
            not isinstance(provider_binding, dict)
            or set(provider_binding) != provider_binding_fields
            or provider_binding.get("provider_id") != payload.get("provider_id")
            or provider_binding.get("profile_version")
            != payload.get("provider_profile_version")
            or provider_binding.get("profile_digest")
            != payload.get("provider_profile_digest")
            or provider_binding.get("selection_mode") != "manual"
            or canonical_digest(
                {
                    key: value
                    for key, value in provider_binding.items()
                    if key != "binding_digest"
                }
            )
            != provider_binding.get("binding_digest")
        ):
            raise OperationalStateError(
                "model_reconciliation_provider_binding_mismatch"
            )
        run_row = connection.execute(
            "SELECT * FROM cognitive_runs WHERE run_id=?",
            (payload["run_id"],),
        ).fetchone()
        if run_row is None:
            raise OperationalStateError("cognitive_run_not_found")
        run = self._run_from_row(connection, run_row)
        authorization_row = connection.execute(
            "SELECT * FROM authorization_artifacts WHERE authorization_id=?",
            (run.authorization_id,),
        ).fetchone()
        if authorization_row is None:
            raise OperationalStateError("model_call_authorization_missing")
        authorization = self._authorization_from_row(authorization_row)
        self._validate_persisted_bindings(connection, authorization)
        if (
            run.authorization_digest != authorization.authority_digest
            or payload["authorization_digest"] != authorization.authority_digest
            or payload["provider_id"] != run.provider_id
            or payload["provider_profile_version"]
            != run.provider_profile_version
            or payload["provider_profile_digest"]
            != run.provider_profile_digest
            or raw_event["project_id"] != authorization.project_id
        ):
            raise OperationalStateError("model_reconciliation_scope_violation")
        return {
            "event": event,
            "receipt": receipt,
            "payload": payload,
            "reservation": reservation_record,
            "run": run,
            "authorization": authorization,
        }

    def open_model_reconciliation_authority(
        self,
        *,
        reservation_event_id: str,
        lease_owner_id: str,
        lease_token: str,
        ttl_ms: int = 300_000,
    ) -> SystemModelReconciliationAuthority:
        if (
            isinstance(ttl_ms, bool)
            or not isinstance(ttl_ms, int)
            or not 1_000 <= ttl_ms <= 900_000
        ):
            raise OperationalStateError("invalid_model_reconciliation_ttl")
        trusted_at = self._trusted_at()
        with closing(self.operational._connect()) as connection:
            self.operational._assert_lease(
                connection,
                owner_id=lease_owner_id,
                token=lease_token,
                at=trusted_at,
            )
            binding = self._model_reservation_binding_on_connection(
                connection,
                reservation_event_id,
            )
            latest = self._latest_checkpoint_on_connection(
                connection,
                binding["run"].run_id,
            )
            self._assert_checkpoint_matches_run(latest, binding["run"])
            state = _model_budget_state_from_payload(latest.model_budget_state)
            reservation_id = binding["payload"]["reservation_id"]
            quarantined = any(
                item.get("kind") == "model_call_quarantine"
                and item.get("reservation_id") == reservation_id
                for item in latest.runtime_recovery_bindings
            )
            if (
                reservation_id in state.settled_reservation_ids
                and not quarantined
            ):
                raise OperationalStateError(
                    "model_reconciliation_reservation_already_settled"
                )
            if (
                reservation_id not in state.settled_reservation_ids
                and (
                    binding["payload"]["request_digest"]
                    not in state.request_digests
                    or state.calls_reserved
                    != len(state.settled_reservation_ids) + 1
                )
            ):
                raise OperationalStateError(
                    "model_reconciliation_reservation_not_pending"
                )
        payload = binding["payload"]
        receipt = binding["receipt"]
        lease_digest = hashlib.sha256(lease_token.encode("utf-8")).hexdigest()
        expires = datetime.fromisoformat(
            trusted_at.replace("Z", "+00:00")
        ) + timedelta(milliseconds=ttl_ms)
        expires_at = normalize_timestamp(expires.isoformat())
        nonce_digest = canonical_digest(
            {
                "reservation_event_id": reservation_event_id,
                "reservation_receipt_digest": receipt.receipt_digest,
                "lease_token_digest": lease_digest,
                "valid_from": trusted_at,
                "expires_at": expires_at,
            }
        )
        identity = {
            "run_id": payload["run_id"],
            "reservation_event_id": reservation_event_id,
            "reservation_receipt_digest": receipt.receipt_digest,
            "reservation_digest": payload["reservation_digest"],
            "lease_token_digest": lease_digest,
            "nonce_digest": nonce_digest,
        }
        return _issue_system_model_reconciliation_authority(
            authority_id=make_identity("modelreconciliation", identity),
            issuer_kind="local_broker",
            issuer_id="runtime:yunos-model-reconciliation",
            project_id=binding["event"].project_id,
            run_id=payload["run_id"],
            original_authorization_digest=payload["authorization_digest"],
            reservation_event_id=reservation_event_id,
            reservation_receipt_digest=receipt.receipt_digest,
            reservation_id=payload["reservation_id"],
            reservation_digest=payload["reservation_digest"],
            request_digest=payload["request_digest"],
            provider_profile_digest=payload["provider_profile_digest"],
            allowed_operations=("quarantine_model_reservation",),
            valid_from=trusted_at,
            expires_at=expires_at,
            max_writes=1,
            lease_owner_id=lease_owner_id,
            lease_token_digest=lease_digest,
            nonce_digest=nonce_digest,
        )

    def quarantine_model_reservation(
        self,
        *,
        reconciliation_authority: SystemModelReconciliationAuthority,
        expected_run_digest: str,
        lease_owner_id: str,
        lease_token: str,
    ) -> dict[str, Any]:
        if type(reconciliation_authority) is not SystemModelReconciliationAuthority:
            raise OperationalStateError(
                "system_model_reconciliation_authority_required"
            )
        reconciliation_authority.assert_factory_sealed()
        trusted_at = self._trusted_at()
        reconciliation_authority.authorize(
            "quarantine_model_reservation",
            trusted_at,
        )
        lease_digest = hashlib.sha256(lease_token.encode("utf-8")).hexdigest()
        if (
            reconciliation_authority.lease_owner_id != lease_owner_id
            or reconciliation_authority.lease_token_digest != lease_digest
        ):
            raise OperationalStateError("model_reconciliation_scope_violation")
        with closing(self.operational._connect(write=True)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self.operational._assert_lease(
                    connection,
                    owner_id=lease_owner_id,
                    token=lease_token,
                    at=trusted_at,
                )
                binding = self._model_reservation_binding_on_connection(
                    connection,
                    reconciliation_authority.reservation_event_id,
                )
                payload = binding["payload"]
                receipt = binding["receipt"]
                reservation_record = binding["reservation"]
                previous = binding["run"]
                authorization = binding["authorization"]
                if (
                    reconciliation_authority.project_id
                    != binding["event"].project_id
                    or reconciliation_authority.run_id != previous.run_id
                    or reconciliation_authority.original_authorization_digest
                    != authorization.authority_digest
                    or reconciliation_authority.reservation_receipt_digest
                    != receipt.receipt_digest
                    or reconciliation_authority.reservation_id
                    != payload["reservation_id"]
                    or reconciliation_authority.reservation_digest
                    != payload["reservation_digest"]
                    or reconciliation_authority.request_digest
                    != payload["request_digest"]
                    or reconciliation_authority.provider_profile_digest
                    != payload["provider_profile_digest"]
                ):
                    raise OperationalStateError(
                        "model_reconciliation_scope_violation"
                    )
                latest = self._latest_checkpoint_on_connection(
                    connection,
                    previous.run_id,
                )
                self._assert_checkpoint_matches_run(latest, previous)
                stable_quarantine = {
                    "kind": "model_call_quarantine",
                    "reservation_event_id": (
                        reconciliation_authority.reservation_event_id
                    ),
                    "reservation_receipt_digest": receipt.receipt_digest,
                    "reservation_id": payload["reservation_id"],
                    "reservation_digest": payload["reservation_digest"],
                    "request_digest": payload["request_digest"],
                    "provider_profile_digest": payload[
                        "provider_profile_digest"
                    ],
                    "outcome": "unknown",
                    "usage_state": "untrusted_unavailable",
                    "cost_accounting": "reservation_ceiling",
                }
                quarantine_digest = canonical_digest(stable_quarantine)
                quarantine_binding = {
                    **stable_quarantine,
                    "quarantine_digest": quarantine_digest,
                }
                existing = tuple(
                    item
                    for item in latest.runtime_recovery_bindings
                    if item.get("kind") == "model_call_quarantine"
                    and item.get("reservation_id") == payload["reservation_id"]
                )
                if existing:
                    if len(existing) != 1 or dict(existing[0]) != quarantine_binding:
                        raise OperationalStateError(
                            "model_quarantine_replay_collision"
                        )
                    state = _model_budget_state_from_payload(
                        latest.model_budget_state
                    )
                    if (
                        payload["reservation_id"]
                        not in state.settled_reservation_ids
                        or payload["reservation_id"]
                        not in state.unknown_cost_reservation_ids
                    ):
                        raise OperationalStateError(
                            "model_quarantine_budget_mismatch"
                        )
                    connection.rollback()
                    return {
                        "status": "quarantined",
                        "reason_code": "model_call_outcome_unknown",
                        "run": previous,
                        "checkpoint": latest,
                        "quarantine_digest": quarantine_digest,
                        "provider_call_allowed": False,
                        "idempotent_replay": True,
                    }
                if previous.run_digest != expected_run_digest:
                    raise OperationalStateError(
                        "cognitive_run_projection_conflict"
                    )
                state = _model_budget_state_from_payload(
                    latest.model_budget_state
                )
                reservation_id = payload["reservation_id"]
                if (
                    reservation_id in state.settled_reservation_ids
                    or payload["request_digest"] not in state.request_digests
                    or state.calls_reserved
                    != len(state.settled_reservation_ids) + 1
                ):
                    raise OperationalStateError(
                        "model_reconciliation_reservation_not_pending"
                    )
                try:
                    next_state = ModelBudgetState(
                        calls_reserved=state.calls_reserved,
                        input_tokens_reserved=state.input_tokens_reserved,
                        output_tokens_reserved=state.output_tokens_reserved,
                        cost_microunits_reserved=state.cost_microunits_reserved,
                        request_digests=state.request_digests,
                        settled_reservation_ids=(
                            *state.settled_reservation_ids,
                            reservation_id,
                        ),
                        actual_input_tokens=(
                            state.actual_input_tokens
                            + reservation_record["input_tokens"]
                        ),
                        actual_output_tokens=(
                            state.actual_output_tokens
                            + reservation_record["output_token_ceiling"]
                        ),
                        actual_cost_microunits=state.actual_cost_microunits,
                        unknown_cost_reserved_ceiling_microunits=(
                            state.unknown_cost_reserved_ceiling_microunits
                            + reservation_record["cost_microunits_ceiling"]
                        ),
                        unknown_cost_reservation_ids=(
                            *state.unknown_cost_reservation_ids,
                            reservation_id,
                        ),
                    )
                except Exception as error:
                    raise OperationalStateError(str(error)) from None
                cancellation_latched = connection.execute(
                    "SELECT 1 FROM state_events WHERE entity_id=? "
                    "AND event_type='cognitive_run_update' "
                    "AND json_extract(canonical_payload,'$.operation')="
                    "'cancellation_latched' LIMIT 1",
                    (previous.run_id,),
                ).fetchone() is not None
                unsafe_reasons = tuple(
                    sorted(
                        {
                            "model_call_outcome_unknown",
                            "model_call_quarantined",
                            *(
                                (
                                    "cancellation_model_observation_pending_reconciliation",
                                )
                                if cancellation_latched
                                else ()
                            ),
                        }
                    )
                )
                checkpoint = _create_cognitive_checkpoint(
                    run_id=previous.run_id,
                    checkpoint_sequence=previous.checkpoint_sequence + 1,
                    safe_to_resume=False,
                    run_status="recovery_required",
                    authorization_digest=previous.authorization_digest,
                    objective_digest=previous.objective_digest,
                    plan_digest=previous.plan_digest,
                    provider_profile_digest=previous.provider_profile_digest,
                    current_node_ids=latest.current_node_ids,
                    completed_node_ids=latest.completed_node_ids,
                    model_budget_state=_model_budget_state_payload(next_state),
                    runtime_recovery_bindings=(
                        *latest.runtime_recovery_bindings,
                        quarantine_binding,
                    ),
                    proposal_ids=latest.proposal_ids,
                    execution_ids=latest.execution_ids,
                    idempotency_keys=latest.idempotency_keys,
                    last_completed_execution_id=None,
                    unsafe_reason_codes=unsafe_reasons,
                    steps_used=previous.steps_used,
                    started_at=previous.started_at,
                    last_activity_at=trusted_at,
                    ended_at=None,
                    failure_code="model_call_outcome_unknown",
                    recorded_at=trusted_at,
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
                    status="recovery_required",
                    checkpoint_sequence=checkpoint.checkpoint_sequence,
                    last_safe_checkpoint=previous.last_safe_checkpoint,
                    model_calls_used=next_state.calls_reserved,
                    tokens_used=(
                        next_state.actual_input_tokens
                        + next_state.actual_output_tokens
                    ),
                    cost_used_microunits=(
                        next_state.actual_cost_microunits
                        + next_state.unknown_cost_reserved_ceiling_microunits
                    ),
                    steps_used=previous.steps_used,
                    started_at=previous.started_at,
                    last_activity_at=trusted_at,
                    ended_at=None,
                    failure_code="model_call_outcome_unknown",
                )
                self._insert_checkpoint(connection, checkpoint)
                self._update_run_row(
                    connection,
                    previous=previous,
                    current=current,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {
            "status": "quarantined",
            "reason_code": "model_call_outcome_unknown",
            "run": current,
            "checkpoint": checkpoint,
            "quarantine_digest": quarantine_digest,
            "provider_call_allowed": False,
            "idempotent_replay": False,
        }

    def reserve_model_call(
        self,
        *,
        run_id: str,
        expected_run_digest: str,
        authorization: CognitiveAuthorizationArtifact,
        reservation: ModelCallReservation,
        provider_id: str,
        provider_profile_version: int,
        provider_profile_digest: str,
        request_digest: str,
        source_authority: AuthorityArtifact,
        lease_owner_id: str,
        lease_token: str,
        budget: ModelBudget | None = None,
    ) -> dict[str, Any]:
        if not isinstance(reservation, ModelCallReservation):
            raise OperationalStateError("typed_model_call_reservation_required")
        if not isinstance(authorization, CognitiveAuthorizationArtifact):
            raise OperationalStateError("invalid_cognitive_authorization")
        validate_sha256(request_digest, "invalid_model_request_digest")
        trusted_at = self._trusted_at()
        self._assert_source_authority(authorization, source_authority)
        authorization.assert_current(trusted_at)
        preliminary_run = self.get_run(run_id)
        if preliminary_run.run_digest != expected_run_digest:
            raise OperationalStateError("cognitive_run_projection_conflict")
        reservation_record = _model_call_reservation_payload(reservation)
        reservation_digest = canonical_digest(reservation_record)
        with closing(self.operational._connect()) as read_connection:
            existing_reservation = read_connection.execute(
                "SELECT e.canonical_payload,e.event_id,r.receipt_digest "
                "FROM state_events e JOIN state_receipts r "
                "ON r.event_id=e.event_id WHERE e.event_type='model_call_reserved' "
                "AND e.idempotency_key=?",
                (reservation.reservation_id,),
            ).fetchone()
        if existing_reservation is not None:
            payload = _decode_canonical(
                existing_reservation["canonical_payload"],
                "model_call_reservation_record_corrupt",
            )
            if (
                payload.get("run_id") != run_id
                or payload.get("authorization_digest")
                != authorization.authority_digest
                or payload.get("reservation_digest") != reservation_digest
                or payload.get("request_digest") != request_digest
            ):
                raise OperationalStateError(
                    "model_call_reservation_replay_collision"
                )
            return {
                "status": "recovery_required",
                "reason_code": "model_call_outcome_unknown",
                "provider_call_allowed": False,
                "reservation_id": reservation.reservation_id,
                "reservation_digest": reservation_digest,
                "reservation_event_id": existing_reservation["event_id"],
                "reservation_receipt_digest": existing_reservation[
                    "receipt_digest"
                ],
                "run": preliminary_run,
                "checkpoint": self.latest_checkpoint(run_id),
                "idempotent_replay": True,
            }
        reservation_entity_id = make_identity(
            "modelreservation",
            {
                "run_id": run_id,
                "reservation_id": reservation.reservation_id,
            },
        )
        reservation_event = self._resource_event(
            source_authority=source_authority,
            project_id=authorization.project_id,
            entity_id=reservation_entity_id,
            event_type="model_call_reserved",
            actor_kind="runtime",
            actor_id="runtime:yunos-cognitive-loop",
            at=trusted_at,
            expires_at=authorization.expires_at,
            payload={
                "run_id": run_id,
                "authorization_digest": authorization.authority_digest,
                "provider_id": provider_id,
                "provider_profile_version": provider_profile_version,
                "provider_profile_digest": provider_profile_digest,
                "request_digest": request_digest,
                "reservation_id": reservation.reservation_id,
                "reservation_digest": reservation_digest,
                "reservation": reservation_record,
                "operation_executed": False,
            },
            idempotency_key=reservation.reservation_id,
            authority_kind="verified_runtime",
            source_kind="observed",
        )
        reservation_preview = self.operational.preview_event(
            authority=source_authority,
            event=reservation_event,
        )
        with closing(self.operational._connect(write=True)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM cognitive_runs WHERE run_id=?",
                    (run_id,),
                ).fetchone()
                if row is None:
                    raise OperationalStateError("cognitive_run_not_found")
                previous = self._run_from_row(connection, row)
                if previous.run_digest != expected_run_digest:
                    raise OperationalStateError(
                        "cognitive_run_projection_conflict"
                    )
                if previous.status not in {
                    "reasoning",
                    "planning",
                    "executing",
                }:
                    raise OperationalStateError(
                        "model_call_run_state_invalid"
                    )
                authorization_row = connection.execute(
                    "SELECT * FROM authorization_artifacts "
                    "WHERE authorization_id=?",
                    (previous.authorization_id,),
                ).fetchone()
                if authorization_row is None:
                    raise OperationalStateError(
                        "model_call_authorization_missing"
                    )
                persisted_authorization = self._authorization_from_row(
                    authorization_row
                )
                if persisted_authorization.to_dict() != authorization.to_dict():
                    raise OperationalStateError(
                        "model_call_authorization_mismatch"
                    )
                self._assert_source_authority(authorization, source_authority)
                self._validate_persisted_bindings(connection, authorization)
                authorization.assert_current(trusted_at)
                if previous.started_at is None:
                    raise OperationalStateError("model_call_run_start_time_missing")
                elapsed_ms = _elapsed_milliseconds(previous.started_at, trusted_at)
                remaining_duration_ms = authorization.max_duration_ms - elapsed_ms
                expiry_remaining_ms = int(
                    (
                        datetime.fromisoformat(
                            authorization.expires_at.replace("Z", "+00:00")
                        )
                        - datetime.fromisoformat(trusted_at.replace("Z", "+00:00"))
                    ).total_seconds()
                    * 1000
                )
                if remaining_duration_ms <= 0 or expiry_remaining_ms <= 0:
                    raise OperationalStateError(
                        "model_call_authorization_duration_exhausted"
                    )
                provider_row = connection.execute(
                    "SELECT canonical_profile,profile_digest FROM provider_registry "
                    "WHERE provider_id=? AND profile_version=?",
                    (provider_id, provider_profile_version),
                ).fetchone()
                if provider_row is None:
                    raise OperationalStateError("model_provider_profile_missing")
                provider_profile = _decode_canonical(
                    provider_row["canonical_profile"],
                    "model_provider_profile_corrupt",
                )
                try:
                    typed_provider_profile = ProviderProfile.from_record(
                        provider_profile
                    )
                except CognitiveProviderError:
                    raise OperationalStateError(
                        "model_provider_profile_corrupt"
                    ) from None
                provider_timeout_ms = typed_provider_profile.timeout_ms
                if (
                    provider_row["profile_digest"] != provider_profile_digest
                    or typed_provider_profile.profile_digest
                    != provider_profile_digest
                    or canonical_json(typed_provider_profile.to_dict())
                    != provider_row["canonical_profile"]
                    or isinstance(provider_timeout_ms, bool)
                    or not isinstance(provider_timeout_ms, int)
                    or provider_timeout_ms < 1
                ):
                    raise OperationalStateError("model_provider_profile_corrupt")
                if provider_timeout_ms > min(
                    remaining_duration_ms,
                    expiry_remaining_ms,
                ):
                    raise OperationalStateError(
                        "model_provider_timeout_exceeds_remaining_authority"
                    )
                binding = reservation.provider_binding
                if (
                    request_digest != reservation.request_digest
                    or provider_id != previous.provider_id
                    or provider_profile_version
                    != previous.provider_profile_version
                    or provider_profile_digest
                    != previous.provider_profile_digest
                    or binding.provider_id != provider_id
                    or binding.profile_version
                    != provider_profile_version
                    or binding.profile_digest
                    != provider_profile_digest
                ):
                    raise OperationalStateError(
                        "model_provider_binding_mismatch"
                    )
                latest = self._latest_checkpoint_on_connection(
                    connection,
                    previous.run_id,
                )
                self._assert_checkpoint_matches_run(latest, previous)
                if not latest.safe_to_resume or latest.unsafe_reason_codes:
                    reason = (
                        "cognitive_model_reservation_pending"
                        if "model_call_reserved_without_settlement"
                        in latest.unsafe_reason_codes
                        else "model_call_prior_outcome_unsettled"
                    )
                    raise OperationalStateError(reason)
                prior_state = _model_budget_state_from_payload(
                    latest.model_budget_state
                )
                if len(prior_state.settled_reservation_ids) != prior_state.calls_reserved:
                    raise OperationalStateError(
                        "cognitive_model_reservation_pending"
                    )
                effective_budget = _bounded_model_budget(
                    authorization=authorization,
                    state=prior_state,
                    reservation=reservation,
                    explicit_budget=budget,
                )
                try:
                    expected_reservation, next_state = (
                        calculate_model_call_reservation(
                            mode="ai",
                            request_digest=reservation.request_digest,
                            provider_binding=reservation.provider_binding,
                            estimated_input_tokens=reservation.input_tokens,
                            requested_output_tokens=reservation.output_token_ceiling,
                            estimated_cost_microunits=(
                                reservation.cost_microunits_ceiling
                            ),
                            provider_context_limit_tokens=(
                                typed_provider_profile.context_limit_tokens
                            ),
                            budget=effective_budget,
                            state=prior_state,
                        )
                    )
                except Exception as error:
                    raise OperationalStateError(str(error)) from None
                if (
                    expected_reservation.request_digest
                    != reservation.request_digest
                    or expected_reservation.provider_binding
                    != reservation.provider_binding
                    or expected_reservation.input_tokens
                    != reservation.input_tokens
                    or expected_reservation.output_token_ceiling
                    != reservation.output_token_ceiling
                    or expected_reservation.cost_microunits_ceiling
                    != reservation.cost_microunits_ceiling
                    or expected_reservation.sequence != reservation.sequence
                    or reservation.grants_authority
                    or reservation.execution_allowed
                ):
                    raise OperationalStateError(
                        "model_call_reservation_binding_mismatch"
                    )
                next_budget_payload = _model_budget_state_payload(next_state)
                reservation_receipt = (
                    self.operational._commit_preview_on_connection(
                        connection,
                        authority=source_authority,
                        preview=reservation_preview,
                        lease_owner_id=lease_owner_id,
                        lease_token=lease_token,
                    )
                )
                if reservation_receipt.idempotent_replay:
                    raise OperationalStateError(
                        "model_call_reservation_replay_collision"
                    )
                self.operational._authorize_auxiliary_write(
                    connection,
                    authority=source_authority,
                    lease_owner_id=lease_owner_id,
                    lease_token=lease_token,
                    project_id=authorization.project_id,
                    entity_id=run_id,
                    operation="cognitive_run_update",
                    at=trusted_at,
                    enforce_for_test=True,
                )
                checkpoint = _create_cognitive_checkpoint(
                    run_id=previous.run_id,
                    checkpoint_sequence=previous.checkpoint_sequence + 1,
                    safe_to_resume=False,
                    run_status=previous.status,
                    authorization_digest=previous.authorization_digest,
                    objective_digest=previous.objective_digest,
                    plan_digest=previous.plan_digest,
                    provider_profile_digest=previous.provider_profile_digest,
                    current_node_ids=latest.current_node_ids,
                    completed_node_ids=latest.completed_node_ids,
                    model_budget_state=next_budget_payload,
                    runtime_recovery_bindings=latest.runtime_recovery_bindings,
                    proposal_ids=latest.proposal_ids,
                    execution_ids=latest.execution_ids,
                    idempotency_keys=latest.idempotency_keys,
                    last_completed_execution_id=latest.last_completed_execution_id,
                    unsafe_reason_codes=(
                        "model_call_reserved_without_settlement",
                    ),
                    steps_used=previous.steps_used,
                    started_at=previous.started_at,
                    last_activity_at=trusted_at,
                    ended_at=None,
                    failure_code=None,
                    recorded_at=trusted_at,
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
                    status=previous.status,
                    checkpoint_sequence=checkpoint.checkpoint_sequence,
                    last_safe_checkpoint=previous.last_safe_checkpoint,
                    model_calls_used=next_state.calls_reserved,
                    tokens_used=(
                        next_state.actual_input_tokens
                        + next_state.actual_output_tokens
                    ),
                    cost_used_microunits=(
                        next_state.actual_cost_microunits
                        + next_state.unknown_cost_reserved_ceiling_microunits
                    ),
                    steps_used=previous.steps_used,
                    started_at=previous.started_at,
                    last_activity_at=trusted_at,
                    ended_at=None,
                    failure_code=None,
                )
                self._insert_checkpoint(connection, checkpoint)
                self._update_run_row(
                    connection,
                    previous=previous,
                    current=current,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {
            "status": "reserved",
            "reservation_id": reservation.reservation_id,
            "reservation_digest": reservation_digest,
            "reservation_event_id": reservation_event.event_id,
            "reservation_receipt_digest": reservation_receipt.receipt_digest,
            "reservation": reservation,
            "budget_state": next_state,
            "run": current,
            "checkpoint": checkpoint,
            "provider_call_allowed": True,
            "idempotent_replay": False,
        }

    def settle_model_call(
        self,
        *,
        run_id: str,
        expected_run_digest: str,
        authorization: CognitiveAuthorizationArtifact,
        reservation: ModelCallReservation,
        outcome: str,
        actual_cost_microunits: int | None,
        runtime_execution_reference: str,
        runtime_receipt_identity: str | None,
        runtime_receipt_digest: str | None,
        runtime_usage_digest: str | None,
        usage: Mapping[str, Any] | None,
        reconciliation_only: bool,
        source_authority: AuthorityArtifact,
        lease_owner_id: str,
        lease_token: str,
        budget: ModelBudget | None = None,
        actual_provider: Mapping[str, Any] | None = None,
        actual_input_tokens: int | None = None,
        actual_output_tokens: int | None = None,
    ) -> dict[str, Any]:
        if not isinstance(reservation, ModelCallReservation):
            raise OperationalStateError("typed_model_call_reservation_required")
        if not isinstance(authorization, CognitiveAuthorizationArtifact):
            raise OperationalStateError("invalid_cognitive_authorization")
        if outcome == "unknown":
            raise OperationalStateError(
                "model_unknown_requires_system_quarantine"
            )
        if outcome not in {"succeeded", "failed"}:
            raise OperationalStateError("invalid_model_call_outcome")
        if not isinstance(reconciliation_only, bool):
            raise OperationalStateError("invalid_reconciliation_mode")
        if reconciliation_only:
            raise OperationalStateError(
                "model_reconciliation_system_authority_required"
            )
        if (
            not isinstance(runtime_execution_reference, str)
            or not runtime_execution_reference
            or len(runtime_execution_reference) > 512
        ):
            raise OperationalStateError("invalid_runtime_execution_reference")
        if runtime_receipt_identity is not None:
            if not isinstance(runtime_receipt_identity, str) or not runtime_receipt_identity:
                raise OperationalStateError("invalid_runtime_receipt_identity")
        if runtime_receipt_digest is not None:
            validate_sha256(
                runtime_receipt_digest,
                "invalid_runtime_receipt_digest",
            )
        if runtime_usage_digest is not None:
            validate_sha256(
                runtime_usage_digest,
                "invalid_runtime_usage_digest",
            )
        if (
            runtime_receipt_identity is None
            or runtime_receipt_digest is None
            or runtime_usage_digest is None
            or not isinstance(usage, Mapping)
        ):
            raise OperationalStateError("model_runtime_receipt_required")
        normalized_usage: dict[str, int] | None = None
        if usage is not None:
            if set(usage) != {
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
            }:
                raise OperationalStateError("invalid_model_runtime_usage")
            normalized_usage = dict(usage)
            if any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                for value in normalized_usage.values()
            ) or normalized_usage["total_tokens"] != (
                normalized_usage["prompt_tokens"]
                + normalized_usage["completion_tokens"]
            ):
                raise OperationalStateError("invalid_model_runtime_usage")
            if canonical_digest(normalized_usage) != runtime_usage_digest:
                raise OperationalStateError("model_runtime_usage_digest_mismatch")
            if actual_input_tokens is None:
                actual_input_tokens = normalized_usage["prompt_tokens"]
            if actual_output_tokens is None:
                actual_output_tokens = normalized_usage["completion_tokens"]
        if actual_provider is None:
            actual_provider = reservation.provider_binding.response_identity()
        trusted_at = self._trusted_at()
        self._assert_source_authority(authorization, source_authority)
        authorization.assert_current(trusted_at)
        settlement_identity = {
            "run_id": run_id,
            "reservation_id": reservation.reservation_id,
            "outcome": outcome,
            "runtime_execution_reference": runtime_execution_reference,
            "runtime_receipt_identity": runtime_receipt_identity,
            "runtime_receipt_digest": runtime_receipt_digest,
            "runtime_usage_digest": runtime_usage_digest,
            "usage": normalized_usage,
            "actual_provider": dict(actual_provider),
            "actual_input_tokens": actual_input_tokens,
            "actual_output_tokens": actual_output_tokens,
            "actual_cost_microunits": actual_cost_microunits,
        }
        settlement_digest = canonical_digest(settlement_identity)
        terminal_event_type = "model_call_settled"
        with closing(self.operational._connect()) as read_connection:
            prior_terminal = read_connection.execute(
                "SELECT canonical_payload,event_id FROM state_events "
                "WHERE event_type=? AND "
                "json_extract(canonical_payload,'$.reservation_id')=? "
                "ORDER BY recorded_at,event_id LIMIT 1",
                (terminal_event_type, reservation.reservation_id),
            ).fetchone()
        if prior_terminal is not None:
            prior_payload = _decode_canonical(
                prior_terminal["canonical_payload"],
                "model_call_terminal_record_corrupt",
            )
            if prior_payload.get("settlement_digest") != settlement_digest:
                raise OperationalStateError(
                    "model_call_settlement_replay_collision"
                )
            loaded_run = self.get_run(run_id)
            return {
                "status": "settled",
                "reason_code": None,
                "reservation": reservation,
                "run": loaded_run,
                "checkpoint": self.latest_checkpoint(run_id),
                "settlement_digest": settlement_digest,
                "runtime_receipt_digest": runtime_receipt_digest,
                "runtime_usage_digest": runtime_usage_digest,
                "idempotent_replay": True,
            }
        terminal_preview = None
        terminal_event = None
        if not reconciliation_only:
            reservation_entity_id = make_identity(
                "modelreservation",
                {
                    "run_id": run_id,
                    "reservation_id": reservation.reservation_id,
                },
            )
            terminal_event = self._resource_event(
                source_authority=source_authority,
                project_id=authorization.project_id,
                entity_id=reservation_entity_id,
                event_type=terminal_event_type,
                actor_kind="runtime",
                actor_id="runtime:yunos-cognitive-loop",
                at=trusted_at,
                expires_at=authorization.expires_at,
                payload={
                    **settlement_identity,
                    "settlement_digest": settlement_digest,
                },
                idempotency_key=make_identity(
                    "idempotency",
                    {
                        "operation": terminal_event_type,
                        "reservation_id": reservation.reservation_id,
                        "settlement_digest": settlement_digest,
                    },
                ),
                authority_kind="verified_runtime",
                source_kind="observed",
            )
            terminal_preview = self.operational.preview_event(
                authority=source_authority,
                event=terminal_event,
            )
        with closing(self.operational._connect(write=True)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM cognitive_runs WHERE run_id=?",
                    (run_id,),
                ).fetchone()
                if row is None:
                    raise OperationalStateError("cognitive_run_not_found")
                previous = self._run_from_row(connection, row)
                if previous.run_digest != expected_run_digest:
                    raise OperationalStateError(
                        "cognitive_run_projection_conflict"
                    )
                authorization_row = connection.execute(
                    "SELECT * FROM authorization_artifacts "
                    "WHERE authorization_id=?",
                    (previous.authorization_id,),
                ).fetchone()
                if authorization_row is None:
                    raise OperationalStateError(
                        "model_call_authorization_missing"
                    )
                persisted_authorization = self._authorization_from_row(
                    authorization_row
                )
                if persisted_authorization.to_dict() != authorization.to_dict():
                    raise OperationalStateError(
                        "model_call_authorization_mismatch"
                    )
                self._validate_persisted_bindings(connection, authorization)
                if not reconciliation_only:
                    authorization.assert_current(trusted_at)
                latest = self._latest_checkpoint_on_connection(
                    connection,
                    previous.run_id,
                )
                self._assert_checkpoint_matches_run(latest, previous)
                state = _model_budget_state_from_payload(
                    latest.model_budget_state
                )
                stored_event = connection.execute(
                    "SELECT e.event_id,e.canonical_payload,r.receipt_digest "
                    "FROM state_events e JOIN state_receipts r "
                    "ON r.event_id=e.event_id "
                    "WHERE e.event_type='model_call_reserved' "
                    "AND e.idempotency_key=?",
                    (reservation.reservation_id,),
                ).fetchone()
                if stored_event is None:
                    raise OperationalStateError(
                        "model_call_reservation_not_persisted"
                    )
                stored_payload = _decode_canonical(
                    stored_event["canonical_payload"],
                    "model_call_reservation_record_corrupt",
                )
                stored = stored_payload.get("reservation")
                if not isinstance(stored, dict):
                    raise OperationalStateError(
                        "model_call_reservation_record_corrupt"
                    )
                expected_record = _model_call_reservation_payload(reservation)
                for key, value in expected_record.items():
                    if key in {
                        "status",
                        "runtime_execution_reference",
                        "runtime_receipt_digest",
                        "actual_input_tokens",
                        "actual_output_tokens",
                        "actual_cost_microunits",
                        "settlement_digest",
                    }:
                        continue
                    if stored.get(key) != value:
                        raise OperationalStateError(
                            "model_call_reservation_binding_mismatch"
                        )
                if (
                    stored_payload.get("run_id") != run_id
                    or stored_payload.get("authorization_digest")
                    != authorization.authority_digest
                    or stored_payload.get("reservation_digest")
                    != canonical_digest(expected_record)
                ):
                    raise OperationalStateError(
                        "model_call_reservation_binding_mismatch"
                    )
                cancellation_latched = connection.execute(
                    "SELECT 1 FROM state_events WHERE entity_id=? "
                    "AND event_type='cognitive_run_update' "
                    "AND json_extract(canonical_payload,'$.operation')="
                    "'cancellation_latched' LIMIT 1",
                    (previous.run_id,),
                ).fetchone() is not None
                effective_budget = _settlement_model_budget(
                    authorization=authorization,
                    state=state,
                    explicit_budget=budget,
                )
                try:
                    next_state = calculate_model_call_settlement(
                        reservation=reservation,
                        state=state,
                        actual_provider=actual_provider,
                        actual_input_tokens=actual_input_tokens,
                        actual_output_tokens=actual_output_tokens,
                        actual_cost_microunits=actual_cost_microunits,
                        budget=effective_budget,
                    )
                except Exception as error:
                    raise OperationalStateError(str(error)) from None
                next_payload = _model_budget_state_payload(next_state)
                if reconciliation_only:
                    self.operational._assert_lease(
                        connection,
                        owner_id=lease_owner_id,
                        token=lease_token,
                        at=trusted_at,
                    )
                else:
                    if terminal_preview is None:
                        raise OperationalStateError(
                            "model_call_settlement_event_missing"
                        )
                    self.operational._commit_preview_on_connection(
                        connection,
                        authority=source_authority,
                        preview=terminal_preview,
                        lease_owner_id=lease_owner_id,
                        lease_token=lease_token,
                    )
                    self.operational._authorize_auxiliary_write(
                        connection,
                        authority=source_authority,
                        lease_owner_id=lease_owner_id,
                        lease_token=lease_token,
                        project_id=authorization.project_id,
                        entity_id=run_id,
                        operation="cognitive_run_update",
                        at=trusted_at,
                        enforce_for_test=True,
                    )
                recovery_binding = {
                    "kind": "model_call",
                    "reservation_id": reservation.reservation_id,
                    "request_digest": reservation.request_digest,
                    "runtime_execution_reference": runtime_execution_reference,
                    "runtime_receipt_identity": runtime_receipt_identity,
                    "runtime_receipt_digest": runtime_receipt_digest,
                    "runtime_usage_digest": runtime_usage_digest,
                    "settlement_digest": settlement_digest,
                }
                checkpoint = _create_cognitive_checkpoint(
                    run_id=previous.run_id,
                    checkpoint_sequence=previous.checkpoint_sequence + 1,
                    safe_to_resume=not cancellation_latched,
                    run_status=(
                        "recovery_required"
                        if cancellation_latched
                        else previous.status
                    ),
                    authorization_digest=previous.authorization_digest,
                    objective_digest=previous.objective_digest,
                    plan_digest=previous.plan_digest,
                    provider_profile_digest=previous.provider_profile_digest,
                    current_node_ids=latest.current_node_ids,
                    completed_node_ids=latest.completed_node_ids,
                    model_budget_state=next_payload,
                    runtime_recovery_bindings=tuple(
                        (*latest.runtime_recovery_bindings, recovery_binding)
                    ),
                    proposal_ids=latest.proposal_ids,
                    execution_ids=latest.execution_ids,
                    idempotency_keys=latest.idempotency_keys,
                    last_completed_execution_id=latest.last_completed_execution_id,
                    unsafe_reason_codes=(
                        ("cancellation_model_observation_pending_reconciliation",)
                        if cancellation_latched
                        else ()
                    ),
                    steps_used=previous.steps_used,
                    started_at=previous.started_at,
                    last_activity_at=trusted_at,
                    ended_at=None,
                    failure_code=(
                        "cancellation_pending_reconciliation"
                        if cancellation_latched
                        else previous.failure_code
                    ),
                    recorded_at=trusted_at,
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
                    status=(
                        "recovery_required"
                        if cancellation_latched
                        else previous.status
                    ),
                    checkpoint_sequence=checkpoint.checkpoint_sequence,
                    last_safe_checkpoint=(
                        previous.last_safe_checkpoint
                        if cancellation_latched
                        else checkpoint.checkpoint_digest
                    ),
                    model_calls_used=next_state.calls_reserved,
                    tokens_used=(
                        next_state.actual_input_tokens
                        + next_state.actual_output_tokens
                    ),
                    cost_used_microunits=(
                        next_state.actual_cost_microunits
                        + next_state.unknown_cost_reserved_ceiling_microunits
                    ),
                    steps_used=previous.steps_used,
                    started_at=previous.started_at,
                    last_activity_at=trusted_at,
                    ended_at=None,
                    failure_code=(
                        "cancellation_pending_reconciliation"
                        if cancellation_latched
                        else previous.failure_code
                    ),
                )
                self._insert_checkpoint(connection, checkpoint)
                self._update_run_row(
                    connection,
                    previous=previous,
                    current=current,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {
            "status": "settled",
            "reservation": reservation,
            "budget_state": next_state,
            "run": current,
            "checkpoint": checkpoint,
            "settlement_digest": settlement_digest,
            "runtime_receipt_digest": runtime_receipt_digest,
            "runtime_usage_digest": runtime_usage_digest,
            "idempotent_replay": False,
        }

    def record_tool_proposal(
        self,
        *,
        proposal: ToolProposal,
        source_authority: AuthorityArtifact,
        lease_owner_id: str,
        lease_token: str,
        runtime_execution_reference: str | None = None,
        runtime_receipt_digest: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(proposal, ToolProposal):
            raise OperationalStateError("invalid_tool_proposal")
        trusted_at = self._trusted_at()
        canonical = proposal.to_dict()
        canonical["runtime_execution_reference"] = runtime_execution_reference
        canonical["runtime_receipt_digest"] = runtime_receipt_digest
        canonical_text = canonical_json(canonical)
        with closing(self.operational._connect(write=True)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                run_row = connection.execute(
                    "SELECT * FROM cognitive_runs WHERE run_id=?",
                    (proposal.run_id,),
                ).fetchone()
                if run_row is None:
                    raise OperationalStateError("cognitive_run_not_found")
                run = self._run_from_row(connection, run_row)
                if (
                    run.plan_id != proposal.plan_id
                    or run.plan_version != proposal.plan_version
                    or run.plan_digest != proposal.plan_digest
                    or run.objective_id != proposal.objective_id
                    or run.objective_digest != proposal.objective_digest
                    or run.authorization_id != proposal.authorization_id
                    or run.authorization_digest != proposal.authorization_digest
                ):
                    raise OperationalStateError("tool_proposal_run_binding_mismatch")
                if run.status != "executing":
                    raise OperationalStateError(
                        "tool_proposal_run_not_executing"
                    )
                latest = self._latest_checkpoint_on_connection(
                    connection,
                    run.run_id,
                )
                if not latest.safe_to_resume or latest.unsafe_reason_codes:
                    raise OperationalStateError(
                        "tool_proposal_run_not_safe_to_resume"
                    )
                authorization_row = connection.execute(
                    "SELECT * FROM authorization_artifacts "
                    "WHERE authorization_id=?",
                    (proposal.authorization_id,),
                ).fetchone()
                if authorization_row is None:
                    raise OperationalStateError(
                        "tool_proposal_authorization_missing"
                    )
                authorization = self._authorization_from_row(authorization_row)
                if (
                    authorization.authority_digest
                    != proposal.authorization_digest
                ):
                    raise OperationalStateError(
                        "tool_proposal_authorization_mismatch"
                    )
                self._assert_source_authority(authorization, source_authority)
                self._validate_persisted_bindings(connection, authorization)
                authorization.assert_current(trusted_at)
                self.operational._authorize_auxiliary_write(
                    connection,
                    authority=source_authority,
                    lease_owner_id=lease_owner_id,
                    lease_token=lease_token,
                    project_id=authorization.project_id,
                    entity_id=proposal.run_id,
                    operation="tool_proposal_record",
                    at=trusted_at,
                    enforce_for_test=True,
                )
                existing = connection.execute(
                    "SELECT canonical_proposal,proposal_digest FROM tool_proposals "
                    "WHERE proposal_id=? OR idempotency_key=?",
                    (proposal.proposal_id, proposal.idempotency_key),
                ).fetchone()
                if existing is not None:
                    if (
                        existing["canonical_proposal"] != canonical_text
                        or existing["proposal_digest"] != proposal.proposal_digest
                    ):
                        raise OperationalStateError("tool_proposal_replay_collision")
                    connection.rollback()
                    return {
                        "proposal_id": proposal.proposal_id,
                        "proposal_digest": proposal.proposal_digest,
                        "idempotent_replay": True,
                    }
                connection.execute(
                    "INSERT INTO tool_proposals("
                    "proposal_id,run_id,objective_id,objective_digest,plan_id,plan_version,"
                    "plan_digest,plan_node_id,capability_id,capability_version,"
                    "capability_digest,authorization_id,authorization_digest,risk_class,"
                    "canonical_typed_arguments,argument_digest,idempotency_key,reason,"
                    "expected_observation,runtime_execution_reference,runtime_receipt_digest,"
                    "status,proposed_at,canonical_proposal,proposal_digest"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        proposal.proposal_id,
                        proposal.run_id,
                        proposal.objective_id,
                        proposal.objective_digest,
                        proposal.plan_id,
                        proposal.plan_version,
                        proposal.plan_digest,
                        proposal.plan_node_id,
                        proposal.capability_id,
                        proposal.capability_version,
                        proposal.capability_digest,
                        proposal.authorization_id,
                        proposal.authorization_digest,
                        proposal.risk_class,
                        canonical_json(dict(proposal.typed_arguments)),
                        proposal.argument_digest,
                        proposal.idempotency_key,
                        proposal.reason,
                        proposal.expected_observation,
                        runtime_execution_reference,
                        runtime_receipt_digest,
                        proposal.status,
                        proposal.proposed_at,
                        canonical_text,
                        proposal.proposal_digest,
                    ),
                )
                checkpoint = _create_cognitive_checkpoint(
                    run_id=run.run_id,
                    checkpoint_sequence=run.checkpoint_sequence + 1,
                    safe_to_resume=True,
                    run_status=run.status,
                    authorization_digest=run.authorization_digest,
                    objective_digest=run.objective_digest,
                    plan_digest=run.plan_digest,
                    provider_profile_digest=run.provider_profile_digest,
                    current_node_ids=latest.current_node_ids,
                    completed_node_ids=latest.completed_node_ids,
                    model_budget_state=dict(latest.model_budget_state),
                    runtime_recovery_bindings=latest.runtime_recovery_bindings,
                    proposal_ids=tuple(
                        sorted(set((*latest.proposal_ids, proposal.proposal_id)))
                    ),
                    execution_ids=latest.execution_ids,
                    idempotency_keys=latest.idempotency_keys,
                    last_completed_execution_id=latest.last_completed_execution_id,
                    unsafe_reason_codes=(),
                    steps_used=run.steps_used,
                    started_at=run.started_at,
                    last_activity_at=trusted_at,
                    ended_at=None,
                    failure_code=None,
                    recorded_at=trusted_at,
                )
                current = _create_cognitive_run(
                    run_id=run.run_id,
                    objective_id=run.objective_id,
                    objective_version=run.objective_version,
                    objective_digest=run.objective_digest,
                    plan_id=run.plan_id,
                    plan_version=run.plan_version,
                    plan_digest=run.plan_digest,
                    provider_id=run.provider_id,
                    provider_profile_version=run.provider_profile_version,
                    provider_profile_digest=run.provider_profile_digest,
                    authorization_id=run.authorization_id,
                    authorization_digest=run.authorization_digest,
                    status=run.status,
                    checkpoint_sequence=checkpoint.checkpoint_sequence,
                    last_safe_checkpoint=checkpoint.checkpoint_digest,
                    model_calls_used=run.model_calls_used,
                    tokens_used=run.tokens_used,
                    cost_used_microunits=run.cost_used_microunits,
                    steps_used=run.steps_used,
                    started_at=run.started_at,
                    last_activity_at=trusted_at,
                    ended_at=None,
                    failure_code=None,
                )
                self._insert_checkpoint(connection, checkpoint)
                self._update_run_row(
                    connection,
                    previous=run,
                    current=current,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {
            "proposal_id": proposal.proposal_id,
            "proposal_digest": proposal.proposal_digest,
            "idempotent_replay": False,
        }

    def get_tool_proposal(self, proposal_id: str) -> ToolProposal:
        with closing(self.operational._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM tool_proposals WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
            if row is None:
                raise OperationalStateError("tool_proposal_not_found")
            return self._proposal_from_row(row)

    @staticmethod
    def _proposal_from_row(row: sqlite3.Row) -> ToolProposal:
        payload = _decode_canonical(
            row["canonical_proposal"], "tool_proposal_record_corrupt"
        )
        proposal = _tool_proposal_from_verified_record(payload)
        if (
            proposal.proposal_digest != row["proposal_digest"]
            or proposal.proposal_id != row["proposal_id"]
            or proposal.run_id != row["run_id"]
            or proposal.idempotency_key != row["idempotency_key"]
            or proposal.argument_digest != row["argument_digest"]
        ):
            raise OperationalStateError("tool_proposal_record_tampered")
        return proposal

    @staticmethod
    def _execution_key(
        *,
        proposal: ToolProposal,
        node_digest: str,
        attempt_number: int,
    ) -> str:
        return make_identity(
            "toolidem",
            {
                "run_id": proposal.run_id,
                "objective_digest": proposal.objective_digest,
                "plan_digest": proposal.plan_digest,
                "node_digest": node_digest,
                "proposal_digest": proposal.proposal_digest,
                "capability_digest": proposal.capability_digest,
                "argument_digest": proposal.argument_digest,
                "authorization_digest": proposal.authorization_digest,
                "attempt_number": attempt_number,
            },
        )

    def reserve_tool_execution(
        self,
        *,
        proposal: ToolProposal,
        preview: ToolExecutionPreview,
        source_authority: AuthorityArtifact,
        authorization: CognitiveAuthorizationArtifact,
        lease_owner_id: str,
        lease_token: str,
    ) -> dict[str, Any]:
        trusted_at = self._trusted_at()
        if not isinstance(preview, ToolExecutionPreview):
            raise OperationalStateError("invalid_tool_execution_preview")
        node_digest = preview.node_digest
        attempt_number = preview.attempt_number
        authorization.assert_current(trusted_at)
        self._assert_source_authority(authorization, source_authority)
        if (
            proposal.authorization_digest != authorization.authority_digest
            or preview.proposal_digest != proposal.proposal_digest
            or preview.authorization_digest != authorization.authority_digest
            or preview.plan_digest != proposal.plan_digest
            or preview.capability_digest != proposal.capability_digest
        ):
            raise OperationalStateError("tool_reservation_authorization_mismatch")
        if not preview.prepared_at <= trusted_at < preview.expires_at:
            raise OperationalStateError("tool_execution_preview_expired")
        execution_key = self._execution_key(
            proposal=proposal,
            node_digest=node_digest,
            attempt_number=attempt_number,
        )
        with closing(self.operational._connect()) as connection:
            terminal = connection.execute(
                "SELECT * FROM tool_executions "
                "WHERE idempotency_key=?",
                (execution_key,),
            ).fetchone()
            if terminal is not None:
                result = self._execution_from_row(terminal)
                if (
                    result.proposal_id != proposal.proposal_id
                    or result.proposal_digest != proposal.proposal_digest
                    or result.run_id != proposal.run_id
                ):
                    raise OperationalStateError(
                        "tool_execution_replay_collision"
                    )
                return {"status": "terminal_replay", "execution": result}
            existing_receipt = connection.execute(
                "SELECT r.receipt_digest,e.canonical_payload,e.event_id "
                "FROM state_receipts r JOIN state_events e ON e.event_id=r.event_id "
                "WHERE r.idempotency_key=?",
                (execution_key,),
            ).fetchone()
            if existing_receipt is not None:
                payload = _decode_canonical(
                    existing_receipt["canonical_payload"],
                    "tool_reservation_record_corrupt",
                )
                if (
                    payload.get("execution_key") != execution_key
                    or payload.get("proposal_digest") != proposal.proposal_digest
                    or payload.get("run_id") != proposal.run_id
                    or payload.get("node_digest") != node_digest
                    or payload.get("authorization_digest")
                    != authorization.authority_digest
                    or payload.get("preview_id") != preview.preview_id
                    or payload.get("preview_digest") != preview.preview_digest
                    or payload.get("sealed_preview") != preview.to_dict()
                ):
                    raise OperationalStateError("tool_reservation_replay_collision")
                return {
                    "status": "recovery_required",
                    "reason_code": "tool_execution_outcome_unknown",
                    "execution_key": execution_key,
                    "reservation_event_id": existing_receipt["event_id"],
                    "reservation_receipt_digest": existing_receipt[
                        "receipt_digest"
                    ],
                }
        entity_id = make_identity(
            "toolreservation",
            {"execution_key": execution_key},
        )
        event = self._resource_event(
            source_authority=source_authority,
            project_id=authorization.project_id,
            entity_id=entity_id,
            event_type="tool_execution_reserved",
            actor_kind="runtime",
            actor_id="runtime:yunos-tool-gateway",
            at=trusted_at,
            expires_at=authorization.expires_at,
            payload={
                "execution_key": execution_key,
                "run_id": proposal.run_id,
                "proposal_id": proposal.proposal_id,
                "proposal_digest": proposal.proposal_digest,
                "node_digest": node_digest,
                "capability_digest": proposal.capability_digest,
                "argument_digest": proposal.argument_digest,
                "authorization_digest": authorization.authority_digest,
                "attempt_number": attempt_number,
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
            authority=source_authority,
            event=event,
        )
        with closing(self.operational._connect(write=True)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                run_row = connection.execute(
                    "SELECT * FROM cognitive_runs WHERE run_id=?",
                    (proposal.run_id,),
                ).fetchone()
                if run_row is None:
                    raise OperationalStateError("cognitive_run_not_found")
                previous = self._run_from_row(connection, run_row)
                if previous.status != "executing":
                    raise OperationalStateError(
                        "tool_reservation_run_not_executing"
                    )
                persisted_proposal_row = connection.execute(
                    "SELECT * FROM tool_proposals WHERE proposal_id=?",
                    (proposal.proposal_id,),
                ).fetchone()
                if persisted_proposal_row is None:
                    raise OperationalStateError(
                        "tool_reservation_proposal_not_persisted"
                    )
                persisted_proposal = self._proposal_from_row(
                    persisted_proposal_row
                )
                if (
                    persisted_proposal.identity_payload()
                    != proposal.identity_payload()
                    or persisted_proposal.proposal_digest
                    != proposal.proposal_digest
                ):
                    raise OperationalStateError(
                        "tool_reservation_proposal_mismatch"
                    )
                authorization_row = connection.execute(
                    "SELECT * FROM authorization_artifacts "
                    "WHERE authorization_id=?",
                    (authorization.authorization_id,),
                ).fetchone()
                if authorization_row is None:
                    raise OperationalStateError(
                        "tool_reservation_authorization_missing"
                    )
                persisted_authorization = self._authorization_from_row(
                    authorization_row
                )
                if (
                    persisted_authorization.to_dict()
                    != authorization.to_dict()
                ):
                    raise OperationalStateError(
                        "tool_reservation_authorization_mismatch"
                    )
                self._validate_persisted_bindings(connection, authorization)
                authorization.assert_current(trusted_at)
                node = connection.execute(
                    "SELECT node_digest,node_kind,state,canonical_node "
                    "FROM plan_nodes "
                    "WHERE plan_id=? AND plan_version=? AND node_id=?",
                    (
                        proposal.plan_id,
                        proposal.plan_version,
                        proposal.plan_node_id,
                    ),
                ).fetchone()
                if node is None or node["node_digest"] != node_digest:
                    raise OperationalStateError(
                        "tool_reservation_plan_node_mismatch"
                    )
                node_payload = _decode_canonical(
                    node["canonical_node"],
                    "tool_reservation_plan_node_corrupt",
                )
                if (
                    canonical_digest(node_payload) != node_digest
                    or node_payload.get("node_kind") != node["node_kind"]
                    or node_payload.get("state") != node["state"]
                    or node["node_kind"] in {"human_gate", "decision"}
                    or node["state"] not in {"ready", "running"}
                ):
                    raise OperationalStateError(
                        "tool_reservation_plan_node_not_executable"
                    )
                latest_before = self._latest_checkpoint_on_connection(
                    connection,
                    previous.run_id,
                )
                dependencies = node_payload.get("dependencies")
                if (
                    not isinstance(dependencies, list)
                    or not set(dependencies).issubset(
                        set(latest_before.completed_node_ids)
                    )
                ):
                    raise OperationalStateError(
                        "tool_reservation_dependency_incomplete"
                    )
                retry_policy = node_payload.get("retry_policy")
                if not isinstance(retry_policy, dict):
                    raise OperationalStateError(
                        "tool_reservation_retry_policy_corrupt"
                    )
                maximum_attempts = retry_policy.get("maximum_attempts")
                equivalent_retry_limit = retry_policy.get(
                    "equivalent_retry_limit"
                )
                retryable_failures = retry_policy.get("retryable_failures")
                backoff_ms = retry_policy.get("backoff_ms")
                if (
                    isinstance(maximum_attempts, bool)
                    or not isinstance(maximum_attempts, int)
                    or isinstance(equivalent_retry_limit, bool)
                    or not isinstance(equivalent_retry_limit, int)
                    or not isinstance(retryable_failures, list)
                    or isinstance(backoff_ms, bool)
                    or not isinstance(backoff_ms, int)
                ):
                    raise OperationalStateError(
                        "tool_reservation_retry_policy_corrupt"
                    )
                if attempt_number > maximum_attempts:
                    raise OperationalStateError(
                        "tool_execution_attempt_exceeds_plan_retry_policy"
                    )
                incoming_edges = connection.execute(
                    "SELECT e.edge_kind,e.from_node_id,n.node_kind,n.state "
                    "FROM plan_edges e JOIN plan_nodes n "
                    "ON n.plan_id=e.plan_id AND n.plan_version=e.plan_version "
                    "AND n.node_id=e.from_node_id "
                    "WHERE e.plan_id=? AND e.plan_version=? "
                    "AND e.to_node_id=?",
                    (
                        proposal.plan_id,
                        proposal.plan_version,
                        proposal.plan_node_id,
                    ),
                ).fetchall()
                for edge in incoming_edges:
                    if edge["edge_kind"] == "resume":
                        if (
                            edge["node_kind"] != "human_gate"
                            or edge["state"] != "completed"
                            or edge["from_node_id"]
                            not in latest_before.completed_node_ids
                        ):
                            raise OperationalStateError(
                                "tool_reservation_human_gate_not_completed"
                            )
                    if edge["edge_kind"] == "decision":
                        ready_targets = connection.execute(
                            "SELECT COUNT(*) FROM plan_edges e "
                            "JOIN plan_nodes n ON n.plan_id=e.plan_id "
                            "AND n.plan_version=e.plan_version "
                            "AND n.node_id=e.to_node_id "
                            "WHERE e.plan_id=? AND e.plan_version=? "
                            "AND e.from_node_id=? AND e.edge_kind='decision' "
                            "AND n.state IN ('ready','running')",
                            (
                                proposal.plan_id,
                                proposal.plan_version,
                                edge["from_node_id"],
                            ),
                        ).fetchone()[0]
                        if (
                            edge["node_kind"] != "decision"
                            or edge["state"] != "completed"
                            or edge["from_node_id"]
                            not in latest_before.completed_node_ids
                            or ready_targets != 1
                        ):
                            raise OperationalStateError(
                                "tool_reservation_decision_not_resolved"
                            )
                if (
                    previous.run_id != proposal.run_id
                    or previous.objective_id != proposal.objective_id
                    or previous.objective_digest != proposal.objective_digest
                    or previous.plan_id != proposal.plan_id
                    or previous.plan_version != proposal.plan_version
                    or previous.plan_digest != proposal.plan_digest
                    or previous.authorization_id
                    != proposal.authorization_id
                    or previous.authorization_digest
                    != proposal.authorization_digest
                ):
                    raise OperationalStateError(
                        "tool_reservation_run_binding_mismatch"
                    )
                reserved_steps = connection.execute(
                    "SELECT COUNT(*) FROM state_events "
                    "WHERE event_type='tool_execution_reserved' "
                    "AND json_extract(canonical_payload,'$.run_id')=?",
                    (previous.run_id,),
                ).fetchone()[0]
                if reserved_steps >= authorization.max_steps:
                    raise OperationalStateError(
                        "tool_authorization_step_budget_exhausted"
                    )
                if previous.started_at is None:
                    raise OperationalStateError(
                        "cognitive_run_start_time_missing"
                    )
                started = datetime.fromisoformat(
                    previous.started_at.replace("Z", "+00:00")
                )
                current_time = datetime.fromisoformat(
                    trusted_at.replace("Z", "+00:00")
                )
                elapsed_ms = int((current_time - started).total_seconds() * 1000)
                if elapsed_ms < 0 or elapsed_ms >= authorization.max_duration_ms:
                    raise OperationalStateError(
                        "tool_authorization_duration_budget_exhausted"
                    )
                if attempt_number == 2:
                    if equivalent_retry_limit < 1:
                        raise OperationalStateError(
                            "tool_equivalent_retry_not_authorized"
                        )
                    prior_key = self._execution_key(
                        proposal=proposal,
                        node_digest=node_digest,
                        attempt_number=1,
                    )
                    prior = connection.execute(
                        "SELECT * FROM tool_executions WHERE idempotency_key=?",
                        (prior_key,),
                    ).fetchone()
                    if prior is None:
                        prior_reservation = connection.execute(
                            "SELECT 1 FROM state_events WHERE idempotency_key=?",
                            (prior_key,),
                        ).fetchone()
                        raise OperationalStateError(
                            "tool_execution_outcome_unknown"
                            if prior_reservation is not None
                            else "tool_execution_attempt_one_required"
                        )
                    prior_result = self._execution_from_row(prior)
                    if (
                        prior_result.status != "failed"
                        or prior_result.verification_status != "failed"
                        or "tool_execution_no_owned_change"
                        not in prior_result.reason_codes
                        or not set(prior_result.reason_codes).intersection(
                            retryable_failures
                        )
                    ):
                        raise OperationalStateError(
                            "tool_equivalent_retry_not_authorized"
                        )
                    prior_finished = datetime.fromisoformat(
                        prior_result.finished_at.replace("Z", "+00:00")
                    )
                    if (
                        int(
                            (current_time - prior_finished).total_seconds()
                            * 1000
                        )
                        < backoff_ms
                    ):
                        raise OperationalStateError(
                            "tool_equivalent_retry_backoff_active"
                        )
                receipt = self.operational._commit_preview_on_connection(
                    connection,
                    authority=source_authority,
                    preview=event_preview,
                    lease_owner_id=lease_owner_id,
                    lease_token=lease_token,
                )
                if receipt.idempotent_replay:
                    connection.rollback()
                    return {
                        "status": "recovery_required",
                        "reason_code": "tool_execution_outcome_unknown",
                        "execution_key": execution_key,
                        "reservation_event_id": receipt.event_id,
                        "reservation_receipt_digest": receipt.receipt_digest,
                    }
                latest = latest_before
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
                        sorted(
                            set((*latest.current_node_ids, proposal.plan_node_id))
                        )
                    ),
                    completed_node_ids=latest.completed_node_ids,
                    model_budget_state=dict(latest.model_budget_state),
                    runtime_recovery_bindings=latest.runtime_recovery_bindings,
                    proposal_ids=tuple(
                        sorted(set((*latest.proposal_ids, proposal.proposal_id)))
                    ),
                    execution_ids=latest.execution_ids,
                    idempotency_keys=tuple(
                        sorted(set((*latest.idempotency_keys, execution_key)))
                    ),
                    last_completed_execution_id=latest.last_completed_execution_id,
                    unsafe_reason_codes=(
                        "tool_execution_reserved_without_terminal",
                    ),
                    steps_used=previous.steps_used,
                    started_at=previous.started_at,
                    last_activity_at=trusted_at,
                    ended_at=None,
                    failure_code=None,
                    recorded_at=trusted_at,
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
                    last_activity_at=trusted_at,
                    ended_at=None,
                    failure_code=None,
                )
                self._insert_checkpoint(connection, checkpoint)
                self._update_run_row(
                    connection,
                    previous=previous,
                    current=current,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {
            "status": "reserved",
            "execution_key": execution_key,
            "reservation_event_id": event.event_id,
            "reservation_receipt_digest": receipt.receipt_digest,
            "attempt_number": attempt_number,
            "preview_id": preview.preview_id,
            "preview_digest": preview.preview_digest,
        }

    @staticmethod
    def _execution_from_row(row: sqlite3.Row) -> ToolExecutionResult:
        payload = _decode_canonical(
            row["canonical_execution"],
            "tool_execution_record_corrupt",
        )
        result = _tool_execution_from_verified_record(payload)
        if (
            result.execution_id != row["execution_id"]
            or result.execution_digest != row["execution_digest"]
            or result.idempotency_key != row["idempotency_key"]
            or result.reservation_event_id != row["reservation_event_id"]
            or result.preview_id != row["preview_id"]
            or result.preview_digest != row["preview_digest"]
        ):
            raise OperationalStateError("tool_execution_record_tampered")
        return result

    def get_execution_by_idempotency(
        self, idempotency_key: str
    ) -> ToolExecutionResult | None:
        with closing(self.operational._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM tool_executions WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            return None if row is None else self._execution_from_row(row)

    def open_reconciliation_authority(
        self,
        *,
        reservation_event_id: str,
        lease_owner_id: str,
        lease_token: str,
        ttl_ms: int = 300_000,
    ) -> SystemReconciliationAuthority:
        if (
            isinstance(ttl_ms, bool)
            or not isinstance(ttl_ms, int)
            or not 1_000 <= ttl_ms <= 900_000
        ):
            raise OperationalStateError("invalid_reconciliation_ttl")
        trusted_at = self._trusted_at()
        with closing(self.operational._connect()) as connection:
            self.operational._assert_lease(
                connection,
                owner_id=lease_owner_id,
                token=lease_token,
                at=trusted_at,
            )
            row = connection.execute(
                "SELECT e.event_id,e.project_id,e.idempotency_key,e.canonical_payload,"
                "r.receipt_digest FROM state_events e "
                "JOIN state_receipts r ON r.event_id=e.event_id "
                "LEFT JOIN tool_executions t ON t.reservation_event_id=e.event_id "
                "WHERE e.event_id=? AND e.event_type='tool_execution_reserved' "
                "AND t.execution_id IS NULL",
                (reservation_event_id,),
            ).fetchone()
            if row is None:
                raise OperationalStateError("reconciliation_receipt_missing")
            payload = _decode_canonical(
                row["canonical_payload"],
                "tool_reservation_record_corrupt",
            )
            sealed_preview = payload.get("sealed_preview")
            if (
                payload.get("state") != "available"
                or payload.get("execution_key") != row["idempotency_key"]
                or not isinstance(sealed_preview, dict)
                or payload.get("preview_digest")
                != sealed_preview.get("preview_digest")
                or canonical_digest(
                    {
                        key: value
                        for key, value in sealed_preview.items()
                        if key != "preview_digest"
                    }
                )
                != payload.get("preview_digest")
            ):
                raise OperationalStateError(
                    "tool_reservation_record_binding_mismatch"
                )
            run = connection.execute(
                "SELECT a.authority_digest AS authorization_digest "
                "FROM cognitive_runs r JOIN authorization_artifacts a "
                "ON a.authorization_id=r.authorization_id WHERE r.run_id=?",
                (payload.get("run_id"),),
            ).fetchone()
            if (
                run is None
                or run["authorization_digest"]
                != payload.get("authorization_digest")
            ):
                raise OperationalStateError("reconciliation_scope_violation")
        lease_digest = hashlib.sha256(lease_token.encode("utf-8")).hexdigest()
        expires = datetime.fromisoformat(
            trusted_at.replace("Z", "+00:00")
        ) + timedelta(milliseconds=ttl_ms)
        expires_at = normalize_timestamp(expires.isoformat())
        nonce_digest = canonical_digest(
            {
                "reservation_event_id": reservation_event_id,
                "reservation_receipt_digest": row["receipt_digest"],
                "lease_token_digest": lease_digest,
                "valid_from": trusted_at,
                "expires_at": expires_at,
            }
        )
        identity = {
            "run_id": payload["run_id"],
            "reservation_event_id": reservation_event_id,
            "reservation_receipt_digest": row["receipt_digest"],
            "lease_token_digest": lease_digest,
            "nonce_digest": nonce_digest,
        }
        return _issue_system_reconciliation_authority(
            authority_id=make_identity("reconciliation", identity),
            issuer_kind="local_broker",
            issuer_id="runtime:yunos-reconciliation",
            project_id=row["project_id"],
            run_id=payload["run_id"],
            original_authorization_digest=payload["authorization_digest"],
            reservation_event_id=reservation_event_id,
            reservation_receipt_digest=row["receipt_digest"],
            execution_key=row["idempotency_key"],
            proposal_digest=payload["proposal_digest"],
            preview_digest=payload["preview_digest"],
            allowed_operations=(
                "record_reserved_terminal",
                "mark_recovery_required",
                "append_recovery_supervision",
            ),
            valid_from=trusted_at,
            expires_at=expires_at,
            max_writes=1,
            lease_owner_id=lease_owner_id,
            lease_token_digest=lease_digest,
            nonce_digest=nonce_digest,
        )

    def reconcile_reserved_execution(
        self,
        *,
        reservation_event_id: str,
        decision: str,
        lease_owner_id: str,
        lease_token: str,
        observed_result: ToolExecutionResult | None = None,
    ) -> dict[str, Any]:
        if decision not in {
            "record_observed_terminal",
            "mark_recovery_required",
        }:
            raise OperationalStateError("reconciliation_scope_violation")
        authority = self.open_reconciliation_authority(
            reservation_event_id=reservation_event_id,
            lease_owner_id=lease_owner_id,
            lease_token=lease_token,
        )
        if decision == "record_observed_terminal":
            if not isinstance(observed_result, ToolExecutionResult):
                raise OperationalStateError(
                    "reconciliation_result_unverifiable"
                )
            return self.record_tool_execution(
                result=observed_result,
                source_authority=None,
                reconciliation_authority=authority,
                lease_owner_id=lease_owner_id,
                lease_token=lease_token,
            )
        if observed_result is not None:
            raise OperationalStateError("reconciliation_scope_violation")
        trusted_at = self._trusted_at()
        authority.authorize("mark_recovery_required", trusted_at)
        with closing(self.operational._connect(write=True)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self.operational._assert_lease(
                    connection,
                    owner_id=lease_owner_id,
                    token=lease_token,
                    at=trusted_at,
                )
                row = connection.execute(
                    "SELECT e.project_id,e.idempotency_key,e.canonical_payload,"
                    "r.receipt_digest FROM state_events e "
                    "JOIN state_receipts r ON r.event_id=e.event_id "
                    "LEFT JOIN tool_executions t "
                    "ON t.reservation_event_id=e.event_id "
                    "WHERE e.event_id=? "
                    "AND e.event_type='tool_execution_reserved' "
                    "AND t.execution_id IS NULL",
                    (reservation_event_id,),
                ).fetchone()
                if row is None:
                    raise OperationalStateError(
                        "reconciliation_receipt_missing"
                    )
                payload = _decode_canonical(
                    row["canonical_payload"],
                    "tool_reservation_record_corrupt",
                )
                if (
                    authority.project_id != row["project_id"]
                    or authority.run_id != payload.get("run_id")
                    or authority.original_authorization_digest
                    != payload.get("authorization_digest")
                    or authority.reservation_receipt_digest
                    != row["receipt_digest"]
                    or authority.execution_key != row["idempotency_key"]
                    or authority.proposal_digest
                    != payload.get("proposal_digest")
                    or authority.preview_digest
                    != payload.get("preview_digest")
                ):
                    raise OperationalStateError(
                        "reconciliation_scope_violation"
                    )
                run_row = connection.execute(
                    "SELECT * FROM cognitive_runs WHERE run_id=?",
                    (authority.run_id,),
                ).fetchone()
                if run_row is None:
                    raise OperationalStateError("cognitive_run_not_found")
                previous = self._run_from_row(connection, run_row)
                latest = self._latest_checkpoint_on_connection(
                    connection,
                    previous.run_id,
                )
                self._assert_checkpoint_matches_run(latest, previous)
                stable_reconciliation = {
                    "kind": "tool_reservation_reconciliation",
                    "decision": "mark_recovery_required",
                    "reservation_event_id": reservation_event_id,
                    "reservation_receipt_digest": row["receipt_digest"],
                    "execution_key": row["idempotency_key"],
                    "authorization_digest": payload["authorization_digest"],
                    "proposal_digest": payload["proposal_digest"],
                    "preview_digest": payload["preview_digest"],
                    "observation_state": "terminal_outcome_unknown",
                    "effect_recorded": False,
                    "terminal_execution_id": None,
                }
                reconciliation_digest = canonical_digest(
                    stable_reconciliation
                )
                reconciliation_binding = {
                    **stable_reconciliation,
                    "reconciliation_digest": reconciliation_digest,
                }
                existing_bindings = tuple(
                    dict(item)
                    for item in latest.runtime_recovery_bindings
                    if item.get("kind")
                    == "tool_reservation_reconciliation"
                    and item.get("reservation_event_id")
                    == reservation_event_id
                )
                if existing_bindings:
                    if (
                        len(existing_bindings) != 1
                        or existing_bindings[0] != reconciliation_binding
                    ):
                        raise OperationalStateError(
                            "tool_reconciliation_replay_collision"
                        )
                    connection.rollback()
                    return {
                        "status": "recovery_required",
                        "run": previous,
                        "checkpoint": latest,
                        "reconciliation_digest": reconciliation_digest,
                        "idempotent_replay": True,
                    }
                if previous.status not in {
                    "executing",
                    "waiting_external",
                    "verifying",
                    "recovery_required",
                }:
                    raise OperationalStateError(
                        "reconciliation_scope_violation"
                    )
                unsafe_reasons = tuple(
                    sorted(
                        {
                            *latest.unsafe_reason_codes,
                            "tool_execution_outcome_unknown",
                            "tool_reservation_reconciled_no_terminal",
                        }
                    )
                )
                checkpoint = _create_cognitive_checkpoint(
                    run_id=previous.run_id,
                    checkpoint_sequence=previous.checkpoint_sequence + 1,
                    safe_to_resume=False,
                    run_status="recovery_required",
                    authorization_digest=previous.authorization_digest,
                    objective_digest=previous.objective_digest,
                    plan_digest=previous.plan_digest,
                    provider_profile_digest=previous.provider_profile_digest,
                    current_node_ids=latest.current_node_ids,
                    completed_node_ids=latest.completed_node_ids,
                    model_budget_state=dict(latest.model_budget_state),
                    runtime_recovery_bindings=(
                        *latest.runtime_recovery_bindings,
                        reconciliation_binding,
                    ),
                    proposal_ids=latest.proposal_ids,
                    execution_ids=latest.execution_ids,
                    idempotency_keys=latest.idempotency_keys,
                    last_completed_execution_id=(
                        latest.last_completed_execution_id
                    ),
                    unsafe_reason_codes=unsafe_reasons,
                    steps_used=previous.steps_used,
                    started_at=previous.started_at,
                    last_activity_at=trusted_at,
                    ended_at=None,
                    failure_code=(
                        previous.failure_code
                        or "tool_execution_outcome_unknown"
                    ),
                    recorded_at=trusted_at,
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
                    status="recovery_required",
                    checkpoint_sequence=checkpoint.checkpoint_sequence,
                    last_safe_checkpoint=previous.last_safe_checkpoint,
                    model_calls_used=previous.model_calls_used,
                    tokens_used=previous.tokens_used,
                    cost_used_microunits=previous.cost_used_microunits,
                    steps_used=previous.steps_used,
                    started_at=previous.started_at,
                    last_activity_at=trusted_at,
                    ended_at=None,
                    failure_code=(
                        previous.failure_code
                        or "tool_execution_outcome_unknown"
                    ),
                )
                self._insert_checkpoint(connection, checkpoint)
                self._update_run_row(
                    connection,
                    previous=previous,
                    current=current,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {
            "status": "recovery_required",
            "run": current,
            "checkpoint": checkpoint,
            "reconciliation_digest": reconciliation_digest,
            "idempotent_replay": False,
        }

    def record_tool_execution(
        self,
        *,
        result: ToolExecutionResult,
        source_authority: AuthorityArtifact | None,
        lease_owner_id: str,
        lease_token: str,
        reconciliation_authority: SystemReconciliationAuthority | None = None,
        current_node_ids: Sequence[str] = (),
        completed_node_ids: Sequence[str] = (),
    ) -> dict[str, Any]:
        if not isinstance(result, ToolExecutionResult):
            raise OperationalStateError("invalid_tool_execution_result")
        if (source_authority is None) == (reconciliation_authority is None):
            raise OperationalStateError(
                "tool_execution_authority_mode_invalid"
            )
        trusted_at = self._trusted_at()
        canonical_text = canonical_json(result.to_dict())
        with closing(self.operational._connect(write=True)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT * FROM tool_executions WHERE idempotency_key=?",
                    (result.idempotency_key,),
                ).fetchone()
                if existing is not None:
                    loaded = self._execution_from_row(existing)
                    if loaded.execution_digest != result.execution_digest:
                        raise OperationalStateError(
                            "tool_execution_replay_collision"
                        )
                    connection.rollback()
                    return {
                        "execution": loaded,
                        "idempotent_replay": True,
                    }
                proposal_row = connection.execute(
                    "SELECT * FROM tool_proposals WHERE proposal_id=?",
                    (result.proposal_id,),
                ).fetchone()
                if proposal_row is None:
                    raise OperationalStateError("tool_execution_proposal_mismatch")
                proposal = self._proposal_from_row(proposal_row)
                if (
                    proposal.proposal_digest != result.proposal_digest
                    or proposal.run_id != result.run_id
                ):
                    raise OperationalStateError("tool_execution_proposal_mismatch")
                node = connection.execute(
                    "SELECT node_digest FROM plan_nodes "
                    "WHERE plan_id=? AND plan_version=? AND node_id=?",
                    (
                        proposal.plan_id,
                        proposal.plan_version,
                        proposal.plan_node_id,
                    ),
                ).fetchone()
                if node is None:
                    raise OperationalStateError("tool_execution_plan_node_missing")
                expected_execution_key = self._execution_key(
                    proposal=proposal,
                    node_digest=node["node_digest"],
                    attempt_number=result.attempt_number,
                )
                if result.idempotency_key != expected_execution_key:
                    raise OperationalStateError(
                        "tool_execution_idempotency_binding_mismatch"
                    )
                if result.policy_decision == "allow":
                    receipt = connection.execute(
                        "SELECT r.receipt_digest,e.idempotency_key,e.event_type,"
                        "e.project_id,e.entity_type,e.canonical_payload "
                        "FROM state_events e JOIN state_receipts r ON r.event_id=e.event_id "
                        "WHERE e.event_id=?",
                        (result.reservation_event_id,),
                    ).fetchone()
                    reservation_payload = (
                        None
                        if receipt is None
                        else _decode_canonical(
                            receipt["canonical_payload"],
                            "tool_reservation_record_corrupt",
                        )
                    )
                    if (
                        receipt is None
                        or receipt["event_type"] != "tool_execution_reserved"
                        or receipt["entity_type"] != "resource"
                        or receipt["receipt_digest"]
                        != result.reservation_receipt_digest
                        or receipt["idempotency_key"] != result.idempotency_key
                        or reservation_payload.get("execution_key")
                        != result.idempotency_key
                        or reservation_payload.get("run_id") != result.run_id
                        or reservation_payload.get("proposal_id")
                        != result.proposal_id
                        or reservation_payload.get("proposal_digest")
                        != result.proposal_digest
                        or reservation_payload.get("node_digest")
                        != node["node_digest"]
                        or reservation_payload.get("authorization_digest")
                        != proposal.authorization_digest
                        or reservation_payload.get("attempt_number")
                        != result.attempt_number
                        or reservation_payload.get("preview_id")
                        != result.preview_id
                        or reservation_payload.get("preview_digest")
                        != result.preview_digest
                    ):
                        raise OperationalStateError(
                            "tool_execution_reservation_mismatch"
                        )
                    sealed_preview = reservation_payload.get("sealed_preview")
                    if (
                        not isinstance(sealed_preview, dict)
                        or canonical_digest(
                            {
                                key: value
                                for key, value in sealed_preview.items()
                                if key != "preview_digest"
                            }
                        )
                        != result.preview_digest
                        or sealed_preview.get("preview_id") != result.preview_id
                        or sealed_preview.get("proposal_digest")
                        != result.proposal_digest
                        or sealed_preview.get("attempt_number")
                        != result.attempt_number
                        or sealed_preview.get("invocation_kind")
                        != result.invocation_kind
                        or sealed_preview.get("resolved_argv")
                        != list(result.resolved_argv)
                        or sealed_preview.get("working_directory")
                        != result.working_directory
                    ):
                        raise OperationalStateError(
                            "tool_execution_preview_binding_mismatch"
                        )
                run_row = connection.execute(
                    "SELECT * FROM cognitive_runs WHERE run_id=?", (result.run_id,)
                ).fetchone()
                if run_row is None:
                    raise OperationalStateError("cognitive_run_not_found")
                previous = self._run_from_row(connection, run_row)
                if previous.status not in {"executing", "recovery_required"}:
                    raise OperationalStateError(
                        "tool_execution_run_state_invalid"
                    )
                if (
                    previous.authorization_id != proposal.authorization_id
                    or previous.authorization_digest
                    != proposal.authorization_digest
                    or previous.plan_id != proposal.plan_id
                    or previous.plan_version != proposal.plan_version
                    or previous.plan_digest != proposal.plan_digest
                ):
                    raise OperationalStateError(
                        "tool_execution_run_binding_mismatch"
                    )
                authorization_row = connection.execute(
                    "SELECT * FROM authorization_artifacts "
                    "WHERE authorization_id=?",
                    (proposal.authorization_id,),
                ).fetchone()
                if authorization_row is None:
                    raise OperationalStateError(
                        "tool_execution_authorization_missing"
                    )
                authorization = self._authorization_from_row(authorization_row)
                self._validate_persisted_bindings(connection, authorization)
                if (
                    result.policy_decision == "allow"
                    and receipt["project_id"] != authorization.project_id
                ):
                    raise OperationalStateError(
                        "tool_execution_reservation_mismatch"
                    )
                if reconciliation_authority is None:
                    if source_authority is None:
                        raise OperationalStateError(
                            "tool_execution_authority_mode_invalid"
                        )
                    self._assert_source_authority(
                        authorization,
                        source_authority,
                    )
                else:
                    reconciliation_authority.authorize(
                        "record_reserved_terminal",
                        trusted_at,
                    )
                    lease_digest = hashlib.sha256(
                        lease_token.encode("utf-8")
                    ).hexdigest()
                    if (
                        reconciliation_authority.project_id
                        != authorization.project_id
                        or reconciliation_authority.run_id != result.run_id
                        or reconciliation_authority.original_authorization_digest
                        != authorization.authority_digest
                        or reconciliation_authority.reservation_event_id
                        != result.reservation_event_id
                        or reconciliation_authority.reservation_receipt_digest
                        != result.reservation_receipt_digest
                        or reconciliation_authority.execution_key
                        != result.idempotency_key
                        or reconciliation_authority.proposal_digest
                        != result.proposal_digest
                        or reconciliation_authority.preview_digest
                        != result.preview_digest
                        or reconciliation_authority.lease_owner_id
                        != lease_owner_id
                        or reconciliation_authority.lease_token_digest
                        != lease_digest
                    ):
                        raise OperationalStateError(
                            "reconciliation_scope_violation"
                        )
                    durable_observation = (
                        result.runtime_receipt_digest is not None
                        or bool(result.artifact_references)
                        or (
                            result.executor_kind == "local_broker"
                            and isinstance(result.output.get("backup_reference"), str)
                        )
                        or (
                            result.status == "failed"
                            and "tool_execution_no_owned_change"
                            in result.reason_codes
                        )
                    )
                    if not durable_observation:
                        raise OperationalStateError(
                            "reconciliation_result_unverifiable"
                        )
                if set(current_node_ids) - {proposal.plan_node_id} or set(
                    completed_node_ids
                ) - {proposal.plan_node_id}:
                    raise OperationalStateError(
                        "tool_execution_checkpoint_node_expansion"
                    )
                if reconciliation_authority is None:
                    self.operational._authorize_auxiliary_write(
                        connection,
                        authority=source_authority,
                        lease_owner_id=lease_owner_id,
                        lease_token=lease_token,
                        project_id=authorization.project_id,
                        entity_id=result.run_id,
                        operation="tool_execution_record",
                        at=trusted_at,
                        enforce_for_test=True,
                    )
                else:
                    self.operational._assert_lease(
                        connection,
                        owner_id=lease_owner_id,
                        token=lease_token,
                        at=trusted_at,
                    )
                connection.execute(
                    "INSERT INTO tool_executions("
                    "execution_id,proposal_id,run_id,idempotency_key,reservation_event_id,"
                    "reservation_receipt_digest,preview_id,preview_digest,attempt_number,policy_decision,"
                    "resolved_argv_json,invocation_kind,executor_kind,reason_codes_json,"
                    "working_directory,status,started_at,finished_at,exit_code,stdout_digest,"
                    "stderr_digest,runtime_execution_reference,runtime_receipt_digest,"
                    "verification_status,canonical_execution,execution_digest,recorded_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        result.execution_id,
                        result.proposal_id,
                        result.run_id,
                        result.idempotency_key,
                        result.reservation_event_id,
                        result.reservation_receipt_digest,
                        result.preview_id,
                        result.preview_digest,
                        result.attempt_number,
                        result.policy_decision,
                        canonical_json(list(result.resolved_argv)),
                        result.invocation_kind,
                        result.executor_kind,
                        canonical_json(list(result.reason_codes)),
                        result.working_directory,
                        result.status,
                        result.started_at,
                        result.finished_at,
                        result.exit_code,
                        result.stdout_digest,
                        result.stderr_digest,
                        result.runtime_execution_reference,
                        result.runtime_receipt_digest,
                        result.verification_status,
                        canonical_text,
                        result.execution_digest,
                        trusted_at,
                    ),
                )
                latest = self._latest_checkpoint_on_connection(
                    connection,
                    previous.run_id,
                )
                cancellation_row = connection.execute(
                    "SELECT canonical_payload FROM state_events "
                    "WHERE entity_id=? AND event_type='cognitive_run_update' "
                    "AND json_extract(canonical_payload,'$.operation')="
                    "'cancellation_latched' ORDER BY recorded_at,event_id LIMIT 1",
                    (previous.run_id,),
                ).fetchone()
                cancellation_latched = cancellation_row is not None
                if cancellation_row is not None:
                    cancellation_payload = _decode_canonical(
                        cancellation_row["canonical_payload"],
                        "cognitive_cancellation_record_corrupt",
                    )
                    cancellation_artifact = cancellation_payload.get(
                        "cancellation_artifact"
                    )
                    if not isinstance(cancellation_artifact, Mapping):
                        raise OperationalStateError(
                            "cognitive_cancellation_record_corrupt"
                        )
                    sealed_cancellation = _cancellation_from_verified_record(
                        cancellation_artifact
                    )
                    if sealed_cancellation.run_id != previous.run_id:
                        raise OperationalStateError(
                            "cognitive_cancellation_record_corrupt"
                        )
                safe = result.status == "denied" and not cancellation_latched
                verification_pending = result.status in {"succeeded", "failed"}
                next_status = (
                    "recovery_required"
                    if cancellation_latched
                    else previous.status
                    if safe
                    else "verifying"
                    if verification_pending
                    else "recovery_required"
                )
                unsafe_reasons = (
                    ("cancellation_terminal_observation_pending_reconciliation",)
                    if cancellation_latched
                    else ()
                    if safe
                    else ("tool_execution_pending_independent_verification",)
                    if verification_pending
                    else ("tool_execution_not_safe_to_resume",)
                )
                next_current_nodes = set(latest.current_node_ids)
                next_completed_nodes = set(latest.completed_node_ids)
                if not safe:
                    next_current_nodes.add(proposal.plan_node_id)
                checkpoint = _create_cognitive_checkpoint(
                    run_id=previous.run_id,
                    checkpoint_sequence=previous.checkpoint_sequence + 1,
                    safe_to_resume=safe,
                    run_status=next_status,
                    authorization_digest=previous.authorization_digest,
                    objective_digest=previous.objective_digest,
                    plan_digest=previous.plan_digest,
                    provider_profile_digest=previous.provider_profile_digest,
                    current_node_ids=tuple(sorted(next_current_nodes)),
                    completed_node_ids=tuple(sorted(next_completed_nodes)),
                    model_budget_state=dict(latest.model_budget_state),
                    runtime_recovery_bindings=latest.runtime_recovery_bindings,
                    proposal_ids=latest.proposal_ids,
                    execution_ids=tuple(
                        sorted(set((*latest.execution_ids, result.execution_id)))
                    ),
                    idempotency_keys=tuple(
                        sorted(
                            set((*latest.idempotency_keys, result.idempotency_key))
                        )
                    ),
                    last_completed_execution_id=(
                        latest.last_completed_execution_id
                    ),
                    unsafe_reason_codes=unsafe_reasons,
                    steps_used=previous.steps_used + 1,
                    started_at=previous.started_at,
                    last_activity_at=trusted_at,
                    ended_at=None,
                    failure_code=(
                        "cancellation_pending_reconciliation"
                        if cancellation_latched
                        else "tool_execution_recovery_required"
                        if next_status == "recovery_required"
                        else None
                    ),
                    recorded_at=trusted_at,
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
                    status=next_status,
                    checkpoint_sequence=checkpoint.checkpoint_sequence,
                    last_safe_checkpoint=(
                        checkpoint.checkpoint_digest
                        if safe
                        else previous.last_safe_checkpoint
                    ),
                    model_calls_used=previous.model_calls_used,
                    tokens_used=previous.tokens_used,
                    cost_used_microunits=previous.cost_used_microunits,
                    steps_used=previous.steps_used + 1,
                    started_at=previous.started_at,
                    last_activity_at=trusted_at,
                    ended_at=None,
                    failure_code=(
                        "cancellation_pending_reconciliation"
                        if cancellation_latched
                        else "tool_execution_recovery_required"
                        if next_status == "recovery_required"
                        else None
                    ),
                )
                self._insert_checkpoint(connection, checkpoint)
                self._update_run_row(
                    connection,
                    previous=previous,
                    current=current,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {"execution": result, "idempotent_replay": False}

    def record_independent_tool_verification(
        self,
        *,
        verification: IndependentToolVerification,
        expected_run_digest: str,
        source_authority: AuthorityArtifact,
        lease_owner_id: str,
        lease_token: str,
    ) -> dict[str, Any]:
        """Persist only a factory-sealed independent observation artifact."""

        if type(verification) is not IndependentToolVerification:
            raise OperationalStateError(
                "sealed_independent_tool_verification_required"
            )
        verification.assert_factory_sealed()
        run_id = verification.run_id
        execution_id = verification.tool_execution_id
        if execution_id is None or verification.plan_node_id is None:
            raise OperationalStateError(
                "tool_independent_verification_binding_required"
            )
        trusted_at = self._trusted_at()
        if verification.verified_at > trusted_at:
            raise OperationalStateError("tool_verification_from_future")
        with closing(self.operational._connect(write=True)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                run_row = connection.execute(
                    "SELECT * FROM cognitive_runs WHERE run_id=?",
                    (run_id,),
                ).fetchone()
                if run_row is None:
                    raise OperationalStateError("cognitive_run_not_found")
                previous = self._run_from_row(connection, run_row)
                if previous.run_digest != expected_run_digest:
                    raise OperationalStateError(
                        "cognitive_run_projection_conflict"
                    )
                execution_row = connection.execute(
                    "SELECT * FROM tool_executions WHERE execution_id=? "
                    "AND run_id=?",
                    (execution_id, run_id),
                ).fetchone()
                if execution_row is None:
                    raise OperationalStateError("tool_execution_not_found")
                execution = self._execution_from_row(execution_row)
                proposal_row = connection.execute(
                    "SELECT * FROM tool_proposals WHERE proposal_id=?",
                    (execution.proposal_id,),
                ).fetchone()
                if proposal_row is None:
                    raise OperationalStateError(
                        "tool_verification_proposal_missing"
                    )
                proposal = self._proposal_from_row(proposal_row)
                if (
                    proposal.run_id != run_id
                    or proposal.proposal_digest != execution.proposal_digest
                    or proposal.authorization_digest
                    != previous.authorization_digest
                    or verification.objective_id != previous.objective_id
                    or verification.plan_node_id != proposal.plan_node_id
                ):
                    raise OperationalStateError(
                        "tool_verification_binding_mismatch"
                    )
                assert_independent_verification_store_binding(
                    verification=verification,
                    proposal=proposal,
                    result=execution,
                )
                authorization_row = connection.execute(
                    "SELECT * FROM authorization_artifacts "
                    "WHERE authorization_id=?",
                    (proposal.authorization_id,),
                ).fetchone()
                if authorization_row is None:
                    raise OperationalStateError(
                        "tool_verification_authorization_missing"
                    )
                authorization = self._authorization_from_row(authorization_row)
                self._assert_source_authority(authorization, source_authority)
                self._validate_persisted_bindings(connection, authorization)
                reservation = connection.execute(
                    "SELECT e.entity_id,e.event_id,e.project_id,e.canonical_payload "
                    "FROM state_events e WHERE e.event_id=?",
                    (execution.reservation_event_id,),
                ).fetchone()
                if reservation is None:
                    raise OperationalStateError(
                        "tool_verification_reservation_missing"
                    )
                reservation_payload = _decode_canonical(
                    reservation["canonical_payload"],
                    "tool_verification_reservation_corrupt",
                )
                owned_change_state = (
                    "none"
                    if execution.status == "failed"
                    and "tool_execution_no_owned_change" in execution.reason_codes
                    else "present"
                    if proposal.capability_id
                    in {
                        "project.file.patch",
                        "project.file.create",
                        "project.temp.dispose_owned",
                        "git.commit.create",
                        "project.change.rollback",
                    }
                    else "none"
                )
                normalized_details = {
                    "run_id": run_id,
                    "tool_execution_id": execution.execution_id,
                    "execution_digest": execution.execution_digest,
                    "proposal_id": proposal.proposal_id,
                    "proposal_digest": proposal.proposal_digest,
                    "preview_id": execution.preview_id,
                    "preview_digest": execution.preview_digest,
                    "independent_verification": (
                        verification.to_operational_details()
                    ),
                    "owned_change_state": owned_change_state,
                }
                if (
                    reservation["project_id"] != authorization.project_id
                    or reservation_payload.get("run_id") != run_id
                    or reservation_payload.get("execution_key")
                    != execution.idempotency_key
                    or reservation_payload.get("preview_digest")
                    != execution.preview_digest
                ):
                    raise OperationalStateError(
                        "tool_verification_reservation_mismatch"
                    )
                existing = connection.execute(
                    "SELECT canonical_verification,verification_digest,result "
                    "FROM state_verifications WHERE "
                    "json_extract(canonical_verification,"
                    "'$.details.tool_execution_id')=?",
                    (execution.execution_id,),
                ).fetchone()
                verification_id = verification.verification_id
                verification_semantics = {
                    "verification_id": verification_id,
                    "entity_id": reservation["entity_id"],
                    "event_id": reservation["event_id"],
                    "verifier_kind": verification.verifier_kind,
                    "result": verification.result,
                    "evidence_digest": verification.evidence_digest,
                    "details": {
                        **normalized_details,
                        "verification_kind": verification.verification_kind,
                        "criteria": list(verification.criteria),
                        "evidence_references": list(
                            verification.evidence_references
                        ),
                    },
                }
                if existing is not None:
                    existing_payload = _decode_canonical(
                        existing["canonical_verification"],
                        "tool_verification_record_corrupt",
                    )
                    existing_digest = canonical_digest(existing_payload)
                    existing_semantics = dict(existing_payload)
                    recorded_at = existing_semantics.pop("recorded_at", None)
                    if (
                        not isinstance(recorded_at, str)
                        or not recorded_at
                        or existing["verification_digest"] != existing_digest
                        or existing_semantics != verification_semantics
                        or existing["result"] != verification.result
                    ):
                        raise OperationalStateError(
                            "tool_verification_replay_collision"
                        )
                    connection.rollback()
                    return {
                        "verification_id": verification_id,
                        "verification_digest": existing_digest,
                        "result": existing["result"],
                        "run": previous,
                        "idempotent_replay": True,
                    }
                if previous.status != "verifying":
                    raise OperationalStateError(
                        "tool_verification_run_state_invalid"
                    )
                authorization.assert_current(trusted_at)
                verification_payload = {
                    **verification_semantics,
                    "recorded_at": trusted_at,
                }
                verification_digest = canonical_digest(verification_payload)
                latest = self._latest_checkpoint_on_connection(
                    connection,
                    previous.run_id,
                )
                if (
                    execution.execution_id not in latest.execution_ids
                    or proposal.plan_node_id not in latest.current_node_ids
                    or latest.safe_to_resume
                    or "tool_execution_pending_independent_verification"
                    not in latest.unsafe_reason_codes
                ):
                    raise OperationalStateError(
                        "tool_verification_checkpoint_mismatch"
                    )
                self.operational._authorize_auxiliary_write(
                    connection,
                    authority=source_authority,
                    lease_owner_id=lease_owner_id,
                    lease_token=lease_token,
                    project_id=authorization.project_id,
                    entity_id=reservation["entity_id"],
                    operation="verification_record",
                    at=trusted_at,
                    enforce_for_test=True,
                )
                connection.execute(
                    "INSERT INTO state_verifications("
                    "verification_id,entity_id,event_id,verifier_kind,result,"
                    "evidence_digest,canonical_verification,"
                    "verification_digest,recorded_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        verification_id,
                        reservation["entity_id"],
                        reservation["event_id"],
                        verification.verifier_kind,
                        verification.result,
                        verification.evidence_digest,
                        canonical_json(verification_payload),
                        verification_digest,
                        trusted_at,
                    ),
                )
                verified_success = (
                    execution.status == "succeeded"
                    and verification.result == "passed"
                )
                verified_no_change_failure = (
                    execution.status == "failed"
                    and verification.result == "failed"
                    and normalized_details.get("owned_change_state") == "none"
                    and "tool_execution_no_owned_change"
                    in execution.reason_codes
                )
                safe = verified_success or verified_no_change_failure
                next_status = "executing" if safe else "recovery_required"
                next_current = set(latest.current_node_ids)
                next_completed = set(latest.completed_node_ids)
                if verified_success:
                    next_current.discard(proposal.plan_node_id)
                    next_completed.add(proposal.plan_node_id)
                else:
                    next_current.add(proposal.plan_node_id)
                checkpoint = _create_cognitive_checkpoint(
                    run_id=previous.run_id,
                    checkpoint_sequence=previous.checkpoint_sequence + 1,
                    safe_to_resume=safe,
                    run_status=next_status,
                    authorization_digest=previous.authorization_digest,
                    objective_digest=previous.objective_digest,
                    plan_digest=previous.plan_digest,
                    provider_profile_digest=previous.provider_profile_digest,
                    current_node_ids=tuple(sorted(next_current)),
                    completed_node_ids=tuple(sorted(next_completed)),
                    model_budget_state=dict(latest.model_budget_state),
                    runtime_recovery_bindings=latest.runtime_recovery_bindings,
                    proposal_ids=latest.proposal_ids,
                    execution_ids=latest.execution_ids,
                    idempotency_keys=latest.idempotency_keys,
                    last_completed_execution_id=(
                        execution.execution_id
                        if verified_success
                        else latest.last_completed_execution_id
                    ),
                    unsafe_reason_codes=(
                        ()
                        if safe
                        else ("independent_tool_verification_failed",)
                    ),
                    steps_used=previous.steps_used,
                    started_at=previous.started_at,
                    last_activity_at=trusted_at,
                    ended_at=None,
                    failure_code=(
                        None
                        if safe
                        else "independent_tool_verification_failed"
                    ),
                    recorded_at=trusted_at,
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
                    status=next_status,
                    checkpoint_sequence=checkpoint.checkpoint_sequence,
                    last_safe_checkpoint=(
                        checkpoint.checkpoint_digest
                        if safe
                        else previous.last_safe_checkpoint
                    ),
                    model_calls_used=previous.model_calls_used,
                    tokens_used=previous.tokens_used,
                    cost_used_microunits=previous.cost_used_microunits,
                    steps_used=previous.steps_used,
                    started_at=previous.started_at,
                    last_activity_at=trusted_at,
                    ended_at=None,
                    failure_code=(
                        None
                        if safe
                        else "independent_tool_verification_failed"
                    ),
                )
                self._insert_checkpoint(connection, checkpoint)
                self._update_run_row(
                    connection,
                    previous=previous,
                    current=current,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {
            "verification_id": verification_id,
            "verification_digest": verification_digest,
            "result": verification.result,
            "run": current,
            "idempotent_replay": False,
        }

    def get_cancellation_request(
        self,
        run_id: str,
    ) -> UserCancellationArtifact | None:
        with closing(self.operational._connect()) as connection:
            rows = connection.execute(
                "SELECT canonical_payload FROM state_events "
                "WHERE entity_id=? AND event_type='cognitive_run_update' "
                "ORDER BY recorded_at,event_id",
                (run_id,),
            ).fetchall()
        found: UserCancellationArtifact | None = None
        for row in rows:
            payload = _decode_canonical(
                row["canonical_payload"],
                "cognitive_cancellation_record_corrupt",
            )
            if payload.get("operation") != "cancellation_latched":
                continue
            raw_artifact = payload.get("cancellation_artifact")
            if not isinstance(raw_artifact, Mapping):
                raise OperationalStateError(
                    "cognitive_cancellation_record_corrupt"
                )
            artifact = _cancellation_from_verified_record(raw_artifact)
            if artifact.run_id != run_id:
                raise OperationalStateError(
                    "cognitive_cancellation_record_corrupt"
                )
            if found is not None and found.canonical_digest != artifact.canonical_digest:
                raise OperationalStateError(
                    "cognitive_cancellation_replay_collision"
                )
            found = artifact
        return found

    def cancellation_requested(self, run_id: str) -> bool:
        return self.get_cancellation_request(run_id) is not None

    def record_cancellation_request(
        self,
        *,
        artifact: UserCancellationArtifact,
        source_authority: AuthorityArtifact,
        lease_owner_id: str,
        lease_token: str,
    ) -> dict[str, Any]:
        if not isinstance(artifact, UserCancellationArtifact):
            raise OperationalStateError("typed_user_cancellation_artifact_required")
        if not isinstance(source_authority, AuthorityArtifact):
            raise OperationalStateError("typed_user_authority_required")
        if (
            source_authority.authority_id != artifact.user_authority_id
            or source_authority.authority_digest != artifact.user_authority_digest
            or source_authority.issued_by != artifact.user_actor
            or canonical_digest(source_authority.identity_payload())
            != source_authority.authority_digest
        ):
            raise OperationalStateError("cancellation_user_authority_mismatch")
        trusted_at = self._trusted_at()
        preliminary = self.get_run(artifact.run_id)
        if preliminary.status == "completed":
            raise OperationalStateError("accepted_run_cannot_be_cancelled")
        existing_cancellation = self.get_cancellation_request(artifact.run_id)
        if existing_cancellation is not None:
            # This path is a read-only acknowledgement of the already durable
            # user event.  Validate the factory seal at the artifact's own
            # issuance instant; a replay must not require the old run digest to
            # equal the post-cancellation projection digest.
            artifact.assert_valid_for(
                project_id=artifact.project_id,
                run_id=artifact.run_id,
                expected_run_digest=artifact.expected_run_digest,
                at=artifact.requested_at,
            )
            if (
                existing_cancellation.canonical_digest
                != artifact.canonical_digest
            ):
                raise OperationalStateError(
                    "cognitive_cancellation_replay_collision"
                )
            return {
                "run": preliminary,
                "cancellation": existing_cancellation,
                "idempotent_replay": True,
            }
        if preliminary.status in {"failed", "cancelled"}:
            raise OperationalStateError(
                "terminal_run_cannot_be_cancelled"
            )
        artifact.assert_valid_for(
            project_id=artifact.project_id,
            run_id=preliminary.run_id,
            expected_run_digest=artifact.expected_run_digest,
            at=trusted_at,
        )
        state_event = self._resource_event(
            source_authority=source_authority,
            project_id=artifact.project_id,
            entity_id=artifact.run_id,
            event_type="cognitive_run_update",
            actor_kind="user",
            actor_id=artifact.user_actor,
            at=artifact.requested_at,
            expires_at=artifact.expires_at,
            payload={
                "operation": "cancellation_latched",
                "cancellation_artifact": artifact.to_dict(),
            },
            idempotency_key=artifact.cancellation_id,
            authority_kind="user",
            source_kind="declared",
        )
        preview = self.operational.preview_event(
            authority=source_authority,
            event=state_event,
        )
        with closing(self.operational._connect(write=True)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT event_id,canonical_payload,payload_digest FROM state_events "
                    "WHERE event_id=? OR idempotency_key=?",
                    (state_event.event_id, state_event.idempotency_key),
                ).fetchone()
                row = connection.execute(
                    "SELECT * FROM cognitive_runs WHERE run_id=?",
                    (artifact.run_id,),
                ).fetchone()
                if row is None:
                    raise OperationalStateError("cognitive_run_not_found")
                previous = self._run_from_row(connection, row)
                latched = connection.execute(
                    "SELECT canonical_payload FROM state_events "
                    "WHERE entity_id=? AND event_type='cognitive_run_update' "
                    "AND json_extract(canonical_payload,'$.operation')="
                    "'cancellation_latched' ORDER BY recorded_at,event_id LIMIT 1",
                    (artifact.run_id,),
                ).fetchone()
                if latched is not None:
                    latched_payload = _decode_canonical(
                        latched["canonical_payload"],
                        "cognitive_cancellation_record_corrupt",
                    )
                    latched_artifact = latched_payload.get(
                        "cancellation_artifact"
                    )
                    if not isinstance(latched_artifact, Mapping):
                        raise OperationalStateError(
                            "cognitive_cancellation_record_corrupt"
                        )
                    durable_cancellation = _cancellation_from_verified_record(
                        latched_artifact
                    )
                    if (
                        durable_cancellation.canonical_digest
                        != artifact.canonical_digest
                    ):
                        raise OperationalStateError(
                            "cognitive_cancellation_replay_collision"
                        )
                    connection.rollback()
                    return {
                        "run": previous,
                        "cancellation": durable_cancellation,
                        "idempotent_replay": True,
                    }
                if existing is not None:
                    if (
                        existing["event_id"] != state_event.event_id
                        or existing["canonical_payload"]
                        != canonical_json(state_event.canonical_payload)
                        or existing["payload_digest"] != state_event.payload_digest
                    ):
                        raise OperationalStateError(
                            "cognitive_cancellation_replay_collision"
                        )
                    connection.rollback()
                    return {
                        "run": previous,
                        "cancellation": artifact,
                        "idempotent_replay": True,
                    }
                if previous.run_digest != artifact.expected_run_digest:
                    raise OperationalStateError("cancellation_run_digest_mismatch")
                if previous.status == "completed":
                    raise OperationalStateError("accepted_run_cannot_be_cancelled")
                if previous.status in {"failed", "cancelled"}:
                    raise OperationalStateError(
                        "terminal_run_cannot_be_cancelled"
                    )
                artifact.assert_valid_for(
                    project_id=artifact.project_id,
                    run_id=previous.run_id,
                    expected_run_digest=previous.run_digest,
                    at=trusted_at,
                )
                receipt = self.operational._commit_preview_on_connection(
                    connection,
                    authority=source_authority,
                    preview=preview,
                    lease_owner_id=lease_owner_id,
                    lease_token=lease_token,
                )
                if receipt.idempotent_replay:
                    raise OperationalStateError(
                        "cognitive_cancellation_replay_collision"
                    )
                latest = self._latest_checkpoint_on_connection(
                    connection,
                    previous.run_id,
                )
                self._assert_checkpoint_matches_run(latest, previous)
                safe_cancel = latest.safe_to_resume and not latest.unsafe_reason_codes
                next_status = "cancelled" if safe_cancel else "recovery_required"
                cancellation_unsafe_reasons = (
                    ()
                    if safe_cancel
                    else tuple(
                        sorted(
                            {
                                *latest.unsafe_reason_codes,
                                "cancellation_pending_reconciliation",
                            }
                        )
                    )
                )
                checkpoint = _create_cognitive_checkpoint(
                    run_id=previous.run_id,
                    checkpoint_sequence=previous.checkpoint_sequence + 1,
                    safe_to_resume=safe_cancel,
                    run_status=next_status,
                    authorization_digest=previous.authorization_digest,
                    objective_digest=previous.objective_digest,
                    plan_digest=previous.plan_digest,
                    provider_profile_digest=previous.provider_profile_digest,
                    current_node_ids=() if safe_cancel else latest.current_node_ids,
                    completed_node_ids=latest.completed_node_ids,
                    model_budget_state=dict(latest.model_budget_state),
                    runtime_recovery_bindings=latest.runtime_recovery_bindings,
                    proposal_ids=latest.proposal_ids,
                    execution_ids=latest.execution_ids,
                    idempotency_keys=latest.idempotency_keys,
                    last_completed_execution_id=(
                        latest.last_completed_execution_id if safe_cancel else None
                    ),
                    unsafe_reason_codes=cancellation_unsafe_reasons,
                    steps_used=previous.steps_used,
                    started_at=previous.started_at,
                    last_activity_at=trusted_at,
                    ended_at=trusted_at if safe_cancel else None,
                    failure_code="user_requested_cancellation",
                    recorded_at=trusted_at,
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
                    status=next_status,
                    checkpoint_sequence=checkpoint.checkpoint_sequence,
                    last_safe_checkpoint=(
                        checkpoint.checkpoint_digest
                        if safe_cancel
                        else previous.last_safe_checkpoint
                    ),
                    model_calls_used=previous.model_calls_used,
                    tokens_used=previous.tokens_used,
                    cost_used_microunits=previous.cost_used_microunits,
                    steps_used=previous.steps_used,
                    started_at=previous.started_at,
                    last_activity_at=trusted_at,
                    ended_at=trusted_at if safe_cancel else None,
                    failure_code="user_requested_cancellation",
                )
                self._insert_checkpoint(connection, checkpoint)
                self._update_run_row(
                    connection,
                    previous=previous,
                    current=current,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {
            "run": current,
            "cancellation": artifact,
            "idempotent_replay": False,
        }

    def record_supervision_event(
        self,
        *,
        event: SupervisionEvent,
        expected_run_digest: str,
        source_authority: AuthorityArtifact,
        lease_owner_id: str,
        lease_token: str,
    ) -> dict[str, Any]:
        """Append one supervision observation to v9 ``state_events``.

        Supervision deliberately reuses the existing append-only event ledger;
        it does not introduce another state table or database.
        """

        if not isinstance(event, SupervisionEvent):
            raise OperationalStateError("invalid_cognitive_supervision_event")
        trusted_at = self._trusted_at()
        if event.observed_at > trusted_at:
            raise OperationalStateError("cognitive_supervision_event_from_future")
        with closing(self.operational._connect(write=True)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                run_row = connection.execute(
                    "SELECT * FROM cognitive_runs WHERE run_id=?",
                    (event.run_id,),
                ).fetchone()
                if run_row is None:
                    raise OperationalStateError("cognitive_run_not_found")
                previous = self._run_from_row(connection, run_row)
                if previous.run_digest != expected_run_digest:
                    raise OperationalStateError(
                        "cognitive_run_projection_conflict"
                    )
                authorization_row = connection.execute(
                    "SELECT * FROM authorization_artifacts "
                    "WHERE authorization_id=?",
                    (previous.authorization_id,),
                ).fetchone()
                if authorization_row is None:
                    raise OperationalStateError(
                        "cognitive_supervision_authorization_missing"
                    )
                authorization = self._authorization_from_row(authorization_row)
                self._assert_source_authority(authorization, source_authority)
                self._validate_persisted_bindings(connection, authorization)
                authorization.assert_current(event.observed_at)
                authorization.assert_current(trusted_at)
                if event.plan_node_id is not None:
                    node = connection.execute(
                        "SELECT 1 FROM plan_nodes WHERE plan_id=? "
                        "AND plan_version=? AND node_id=?",
                        (
                            previous.plan_id,
                            previous.plan_version,
                            event.plan_node_id,
                        ),
                    ).fetchone()
                    if node is None:
                        raise OperationalStateError(
                            "cognitive_supervision_plan_node_mismatch"
                        )
                if event.tool_execution_id is not None:
                    execution = connection.execute(
                        "SELECT p.plan_node_id FROM tool_executions x "
                        "JOIN tool_proposals p ON p.proposal_id=x.proposal_id "
                        "WHERE x.execution_id=? AND x.run_id=?",
                        (event.tool_execution_id, event.run_id),
                    ).fetchone()
                    if execution is None or (
                        event.plan_node_id is not None
                        and execution["plan_node_id"] != event.plan_node_id
                    ):
                        raise OperationalStateError(
                            "cognitive_supervision_tool_execution_mismatch"
                        )
                state_event = self._resource_event(
                    source_authority=source_authority,
                    project_id=authorization.project_id,
                    entity_id=event.run_id,
                    event_type="cognitive_run_update",
                    actor_kind="runtime",
                    actor_id="runtime:cognitive-supervisor",
                    at=event.observed_at,
                    expires_at=None,
                    payload={
                        "operation": "supervision_record",
                        "supervision_event": event.to_dict(),
                    },
                    idempotency_key=make_identity(
                        "idempotency",
                        {
                            "operation": "supervision_record",
                            "event_digest": event.event_digest,
                        },
                    ),
                    authority_kind="verified_runtime",
                    source_kind="observed",
                )
                existing = connection.execute(
                    "SELECT event_id,canonical_payload,payload_digest "
                    "FROM state_events WHERE event_id=? OR idempotency_key=?",
                    (state_event.event_id, state_event.idempotency_key),
                ).fetchone()
                if existing is not None:
                    if (
                        existing["event_id"] != state_event.event_id
                        or existing["canonical_payload"]
                        != canonical_json(state_event.canonical_payload)
                        or existing["payload_digest"]
                        != state_event.payload_digest
                    ):
                        raise OperationalStateError(
                            "cognitive_supervision_replay_collision"
                        )
                    connection.rollback()
                    return {
                        "event_id": state_event.event_id,
                        "event_digest": event.event_digest,
                        "run": previous,
                        "idempotent_replay": True,
                    }
                rows = connection.execute(
                    "SELECT canonical_payload FROM state_events "
                    "WHERE entity_id=? AND event_type='cognitive_run_update'",
                    (event.run_id,),
                ).fetchall()
                supervision_count = 0
                for row in rows:
                    payload = _decode_canonical(
                        row["canonical_payload"],
                        "cognitive_supervision_record_corrupt",
                    )
                    if payload.get("operation") == "supervision_record":
                        supervision_count += 1
                if event.sequence != supervision_count + 1:
                    raise OperationalStateError(
                        "cognitive_supervision_sequence_conflict"
                    )
                preview = self.operational.preview_event(
                    authority=source_authority,
                    event=state_event,
                )
                self.operational._commit_preview_on_connection(
                    connection,
                    authority=source_authority,
                    preview=preview,
                    lease_owner_id=lease_owner_id,
                    lease_token=lease_token,
                )
                latest = self._latest_checkpoint_on_connection(
                    connection,
                    previous.run_id,
                )
                checkpoint = _create_cognitive_checkpoint(
                    run_id=previous.run_id,
                    checkpoint_sequence=previous.checkpoint_sequence + 1,
                    safe_to_resume=latest.safe_to_resume,
                    run_status=previous.status,
                    authorization_digest=previous.authorization_digest,
                    objective_digest=previous.objective_digest,
                    plan_digest=previous.plan_digest,
                    provider_profile_digest=previous.provider_profile_digest,
                    current_node_ids=latest.current_node_ids,
                    completed_node_ids=latest.completed_node_ids,
                    model_budget_state=dict(latest.model_budget_state),
                    runtime_recovery_bindings=latest.runtime_recovery_bindings,
                    proposal_ids=latest.proposal_ids,
                    execution_ids=latest.execution_ids,
                    idempotency_keys=latest.idempotency_keys,
                    last_completed_execution_id=latest.last_completed_execution_id,
                    unsafe_reason_codes=latest.unsafe_reason_codes,
                    steps_used=previous.steps_used,
                    started_at=previous.started_at,
                    last_activity_at=trusted_at,
                    ended_at=previous.ended_at,
                    failure_code=previous.failure_code,
                    recorded_at=trusted_at,
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
                    status=previous.status,
                    checkpoint_sequence=checkpoint.checkpoint_sequence,
                    last_safe_checkpoint=(
                        checkpoint.checkpoint_digest
                        if checkpoint.safe_to_resume
                        else previous.last_safe_checkpoint
                    ),
                    model_calls_used=previous.model_calls_used,
                    tokens_used=previous.tokens_used,
                    cost_used_microunits=previous.cost_used_microunits,
                    steps_used=previous.steps_used,
                    started_at=previous.started_at,
                    last_activity_at=trusted_at,
                    ended_at=previous.ended_at,
                    failure_code=previous.failure_code,
                )
                self._insert_checkpoint(connection, checkpoint)
                self._update_run_row(
                    connection,
                    previous=previous,
                    current=current,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {
            "event_id": state_event.event_id,
            "event_digest": event.event_digest,
            "run": current,
            "idempotent_replay": False,
        }

    def list_supervision_events(
        self,
        run_id: str,
    ) -> tuple[SupervisionEvent, ...]:
        with closing(self.operational._connect()) as connection:
            run = connection.execute(
                "SELECT 1 FROM cognitive_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise OperationalStateError("cognitive_run_not_found")
            rows = connection.execute(
                "SELECT canonical_payload FROM state_events "
                "WHERE entity_id=? AND event_type='cognitive_run_update' "
                "ORDER BY recorded_at,event_id",
                (run_id,),
            ).fetchall()
        events: list[SupervisionEvent] = []
        for row in rows:
            payload = _decode_canonical(
                row["canonical_payload"],
                "cognitive_supervision_record_corrupt",
            )
            if payload.get("operation") != "supervision_record":
                continue
            try:
                supervision = SupervisionEvent.from_dict(
                    payload["supervision_event"]
                )
            except (KeyError, TypeError, ValueError, OperationalStateError):
                raise OperationalStateError(
                    "cognitive_supervision_record_corrupt"
                ) from None
            events.append(supervision)
        events.sort(key=lambda item: item.sequence)
        if tuple(item.sequence for item in events) != tuple(
            range(1, len(events) + 1)
        ):
            raise OperationalStateError("cognitive_supervision_sequence_corrupt")
        return tuple(events)

    def latest_checkpoint(self, run_id: str) -> CognitiveRunCheckpoint:
        with closing(self.operational._connect()) as connection:
            return self._latest_checkpoint_on_connection(connection, run_id)

    def list_tool_executions(self, run_id: str) -> tuple[ToolExecutionResult, ...]:
        with closing(self.operational._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM tool_executions WHERE run_id=? "
                "ORDER BY recorded_at,execution_id",
                (run_id,),
            ).fetchall()
            return tuple(self._execution_from_row(row) for row in rows)


__all__ = ["CognitiveExecutionStore", "EXECUTION_STORE_VERSION"]
