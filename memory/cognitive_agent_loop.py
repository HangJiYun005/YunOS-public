"""Finite, fail-closed orchestration for YunOS v10 bounded agency.

The loop in this module is deliberately not a scheduler and never owns a
background thread.  A caller supplies one typed Runtime-bridge turn at a time;
the loop persists the model-call reservation before the bridge may send, binds
the untrusted structured proposal to the already persisted Plan and
Authorization, executes only through :mod:`memory.cognitive_tool_gateway`, and
requires an independent observation before a Plan node is completed.
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping, Protocol, Sequence

from memory.cognitive_authorization import CognitiveAuthorizationArtifact
from memory.cognitive_cancellation import UserCancellationArtifact
from memory.cognitive_execution_models import (
    CognitiveRun,
    ToolExecutionPreview,
    ToolExecutionResult,
    ToolProposal,
    _create_tool_execution_preview,
    create_tool_proposal,
)
from memory.cognitive_execution_store import CognitiveExecutionStore
from memory.cognitive_orchestration import (
    CognitiveModelRequest,
    ModelBudgetState,
    ModelCallReservation,
    ParsedModelOutput,
)
from memory.cognitive_planning import PlanGraph, PlanNode
from memory.cognitive_planning_store import CognitivePlanningStore
from memory.cognitive_supervision import SupervisionEvent, create_supervision_event
from memory.cognitive_tool_gateway import CognitiveToolGateway
from memory.cognitive_verification import (
    CognitiveIndependentVerifier,
    IndependentToolVerification,
)
from memory.operational_state_models import (
    AuthorityArtifact,
    OperationalStateError,
    canonical_digest,
    canonical_json,
    normalize_timestamp,
    utc_now,
    validate_sha256,
)


AGENT_LOOP_VERSION = "v10-d-bounded-agent-loop-2"

_PLAN_FIELDS = {
    "plan_id",
    "objective_id",
    "objective_version",
    "objective_digest",
    "nodes",
    "edges",
    "revisions",
    "entry_node_ids",
    "terminal_node_ids",
    "plan_digest",
    "version",
}
_BUDGET_STATE_FIELDS = {
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
_WRITE_CAPABILITIES = frozenset(
    {
        "project.file.patch",
        "project.file.create",
        "project.temp.dispose_owned",
        "git.commit.create",
        "project.change.rollback",
    }
)
_NEXT_VERSION_PATTERN = re.compile(
    r"(?<![a-z0-9])v\s*(?:10\s*[-._]\s*g|10\s*\.\s*1|11)(?![a-z0-9])"
)
_FORBIDDEN_TEXT_PATTERN = re.compile(
    r"(?<![a-z0-9])(?:"
    r"git\s*(?:merge|tag|push)|force\s*push|reset\s*-*\s*hard|"
    r"powershell|arbitrary\s*shell|shell\s*(?:execute|command)|"
    r"credential|secret|database\s*write|network\s*(?:request|access)"
    r")(?![a-z0-9])"
)


class CognitiveAgentLoopError(OperationalStateError):
    """A stable, non-secret failure from the finite agent controller."""

    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


def _fail(reason_code: str) -> None:
    raise CognitiveAgentLoopError(reason_code)


def _budget_state(value: Mapping[str, Any]) -> ModelBudgetState:
    if not isinstance(value, Mapping) or set(value) != _BUDGET_STATE_FIELDS:
        _fail("agent_persisted_model_budget_invalid")
    payload = dict(value)
    for field in (
        "request_digests",
        "settled_reservation_ids",
        "unknown_cost_reservation_ids",
    ):
        item = payload[field]
        if isinstance(item, (str, bytes)) or not isinstance(item, Sequence):
            _fail("agent_persisted_model_budget_invalid")
        payload[field] = tuple(item)
    try:
        return ModelBudgetState(**payload)
    except Exception:
        _fail("agent_persisted_model_budget_invalid")


def _reason(error: BaseException) -> str:
    value = getattr(error, "reason_code", None)
    if isinstance(value, str) and value and len(value) <= 191:
        return value
    text = str(error)
    if re.fullmatch(r"[a-z0-9_:-]{1,191}", text):
        return text
    return "agent_internal_failure"


def _security_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = "".join(
        "-" if unicodedata.category(character) == "Pd" else character
        for character in normalized
    )
    return " ".join(normalized.split())


def _elapsed_milliseconds(started_at: str, current_at: str) -> int:
    try:
        started = datetime.fromisoformat(
            normalize_timestamp(started_at).replace("Z", "+00:00")
        )
        current = datetime.fromisoformat(
            normalize_timestamp(current_at).replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        _fail("agent_run_time_boundary_invalid")
    elapsed = int((current - started).total_seconds() * 1000)
    if elapsed < 0:
        _fail("agent_run_time_boundary_invalid")
    return elapsed


@dataclass(frozen=True)
class AgentLoopLimits:
    """A caller may tighten, but never widen, an Authorization budget."""

    maximum_steps: int
    maximum_duration_ms: int
    maximum_model_calls: int

    def __post_init__(self) -> None:
        for value, maximum, code in (
            (self.maximum_steps, 10_000, "invalid_agent_step_limit"),
            (self.maximum_duration_ms, 86_400_000, "invalid_agent_duration_limit"),
            (self.maximum_model_calls, 10_000, "invalid_agent_model_call_limit"),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                or value > maximum
            ):
                _fail(code)


@dataclass(frozen=True)
class PreparedRuntimeBridgeTurn:
    """One precomputed, typed request; it carries no execution authority."""

    request: CognitiveModelRequest
    reservation: ModelCallReservation
    budget_state_after_reservation: ModelBudgetState
    bridge: Any
    runtime_authority: Any
    request_id: str
    mutation_id: str
    consent_factory: Callable[[Any], Any]

    def __post_init__(self) -> None:
        if not isinstance(self.request, CognitiveModelRequest):
            _fail("agent_typed_model_request_required")
        if not isinstance(self.reservation, ModelCallReservation):
            _fail("agent_typed_model_reservation_required")
        if not isinstance(self.budget_state_after_reservation, ModelBudgetState):
            _fail("agent_typed_model_budget_state_required")
        if (
            self.request.request_digest != self.reservation.request_digest
            or self.request.provider_binding != self.reservation.provider_binding
            or self.reservation.grants_authority
            or self.reservation.execution_allowed
        ):
            _fail("agent_model_turn_binding_mismatch")
        if self.request.expected_output_kind != "tool_proposal":
            _fail("agent_model_turn_must_request_tool_proposal")
        if (
            not isinstance(self.request_id, str)
            or not 8 <= len(self.request_id) <= 191
            or not isinstance(self.mutation_id, str)
            or not 8 <= len(self.mutation_id) <= 191
            or not callable(self.consent_factory)
        ):
            _fail("agent_model_turn_identity_invalid")
        for name in (
            "prepare_disclosure",
            "preview",
            "execute",
            "read_model_observation",
            "reconcile_model_run",
        ):
            if not callable(getattr(self.bridge, name, None)):
                _fail("agent_runtime_bridge_contract_missing")


class RuntimeBridgeTurnSource(Protocol):
    """Build one deterministic request for the specific persisted Plan node."""

    def prepare_turn(
        self,
        *,
        run: CognitiveRun,
        plan: PlanGraph,
        node: PlanNode,
        authorization: CognitiveAuthorizationArtifact,
        budget_state: ModelBudgetState,
    ) -> PreparedRuntimeBridgeTurn:
        ...


@dataclass(frozen=True)
class AgentLoopOutcome:
    run: CognitiveRun
    reason_code: str | None
    proposal_ids: tuple[str, ...]
    execution_ids: tuple[str, ...]
    verification_ids: tuple[str, ...]
    supervision_events: tuple[SupervisionEvent, ...]
    model_sends: int
    user_accepted: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.run, CognitiveRun):
            _fail("invalid_agent_loop_outcome_run")
        if self.user_accepted is not False:
            _fail("agent_cannot_accept_own_result")
        if self.run.status == "completed":
            _fail("agent_loop_cannot_complete_without_user_acceptance")


class BoundedCognitiveAgentLoop:
    """Run a finite Plan to ``waiting_acceptance`` or a fail-closed state."""

    def __init__(
        self,
        *,
        execution_store: CognitiveExecutionStore,
        planning_store: CognitivePlanningStore,
        tool_gateway: CognitiveToolGateway,
        independent_verifier: CognitiveIndependentVerifier,
        model_turn_source: RuntimeBridgeTurnSource,
        source_authority: AuthorityArtifact,
        lease_owner_id: str,
        lease_token: str,
        limits: AgentLoopLimits,
        trusted_clock: Callable[[], str] | None = None,
        monotonic_clock: Callable[[], float] | None = None,
    ) -> None:
        if not isinstance(execution_store, CognitiveExecutionStore):
            _fail("invalid_agent_execution_store")
        if not isinstance(planning_store, CognitivePlanningStore):
            _fail("invalid_agent_planning_store")
        if not isinstance(tool_gateway, CognitiveToolGateway):
            _fail("invalid_agent_tool_gateway")
        if not isinstance(independent_verifier, CognitiveIndependentVerifier):
            _fail("invalid_agent_independent_verifier")
        if not isinstance(source_authority, AuthorityArtifact):
            _fail("invalid_agent_source_authority")
        if not isinstance(limits, AgentLoopLimits):
            _fail("invalid_agent_loop_limits")
        if not callable(getattr(model_turn_source, "prepare_turn", None)):
            _fail("invalid_agent_model_turn_source")
        if tool_gateway.execution_store is not execution_store:
            _fail("agent_gateway_store_mismatch")
        self.execution_store = execution_store
        self.planning_store = planning_store
        self.tool_gateway = tool_gateway
        self.independent_verifier = independent_verifier
        self.model_turn_source = model_turn_source
        self.source_authority = source_authority
        self.lease_owner_id = lease_owner_id
        self.lease_token = lease_token
        self.limits = limits
        self._trusted_clock = trusted_clock or utc_now
        self._monotonic_clock = monotonic_clock or time.monotonic

    def _now(self) -> str:
        return normalize_timestamp(self._trusted_clock())

    @staticmethod
    def _load_plan(
        planning_store: CognitivePlanningStore,
        run: CognitiveRun,
    ) -> PlanGraph:
        record = planning_store.get_plan(run.plan_id, run.plan_version)
        if record is None or not _PLAN_FIELDS.issubset(record):
            _fail("agent_persisted_plan_missing")
        try:
            plan = PlanGraph.from_dict(
                {field: record[field] for field in _PLAN_FIELDS}
            )
        except Exception:
            _fail("agent_persisted_plan_invalid")
        if (
            plan.plan_digest != run.plan_digest
            or plan.objective_id != run.objective_id
            or plan.objective_digest != run.objective_digest
        ):
            _fail("agent_run_plan_binding_mismatch")
        return plan

    @staticmethod
    def _assert_authorization_boundary(
        run: CognitiveRun,
        plan: PlanGraph,
        authorization: CognitiveAuthorizationArtifact,
        now: str,
    ) -> None:
        authorization.assert_current(now)
        if (
            run.authorization_id != authorization.authorization_id
            or run.authorization_digest != authorization.authority_digest
            or authorization.plan_id != plan.plan_id
            or authorization.plan_version != plan.version
            or authorization.plan_digest != plan.plan_digest
            or authorization.objective_id != plan.objective_id
            or authorization.objective_digest != plan.objective_digest
            or authorization.merge_permission
            or authorization.tag_permission
            or authorization.push_permission
            or authorization.network_permission
            or authorization.next_version_creation_permission
            or authorization.persistent_background_agent_permission
        ):
            _fail("agent_authorization_boundary_violation")

    def _effective_limits(
        self,
        authorization: CognitiveAuthorizationArtifact,
    ) -> AgentLoopLimits:
        return AgentLoopLimits(
            maximum_steps=min(self.limits.maximum_steps, authorization.max_steps),
            maximum_duration_ms=min(
                self.limits.maximum_duration_ms,
                authorization.max_duration_ms,
            ),
            maximum_model_calls=min(
                self.limits.maximum_model_calls,
                authorization.max_model_calls,
            ),
        )

    @staticmethod
    def _assert_persisted_time_boundary(
        run: CognitiveRun,
        authorization: CognitiveAuthorizationArtifact,
        limits: AgentLoopLimits,
        now: str,
    ) -> None:
        authorization.assert_current(now)
        if run.started_at is None:
            _fail("agent_run_start_time_missing")
        elapsed_ms = _elapsed_milliseconds(run.started_at, now)
        if elapsed_ms >= limits.maximum_duration_ms:
            _fail("agent_duration_budget_exhausted")

    @staticmethod
    def _required_nodes(plan: PlanGraph) -> tuple[PlanNode, ...]:
        return tuple(node for node in plan.nodes if node.state != "omitted")

    @classmethod
    def _next_node(
        cls,
        plan: PlanGraph,
        completed_node_ids: Sequence[str],
    ) -> PlanNode | None:
        completed = set(completed_node_ids)
        candidates = []
        for node in cls._required_nodes(plan):
            if node.node_id in completed:
                continue
            if node.node_kind in {"human_gate", "decision"}:
                continue
            if node.state not in {"ready", "running"}:
                continue
            if set(node.dependencies).issubset(completed):
                candidates.append(node)
        return sorted(candidates, key=lambda item: item.node_id)[0] if candidates else None

    @classmethod
    def _authority_blocker(
        cls,
        plan: PlanGraph,
        completed_node_ids: Sequence[str],
    ) -> PlanNode | None:
        completed = set(completed_node_ids)
        candidates = [
            node
            for node in cls._required_nodes(plan)
            if node.node_id not in completed
            and node.node_kind in {"human_gate", "decision"}
            and set(node.dependencies).issubset(completed)
        ]
        return sorted(candidates, key=lambda item: item.node_id)[0] if candidates else None

    @staticmethod
    def _model_proposal(parsed: ParsedModelOutput) -> dict[str, Any]:
        if (
            not isinstance(parsed, ParsedModelOutput)
            or parsed.output_kind != "tool_proposal"
            or parsed.trust_class != "untrusted_model_output"
            or parsed.grants_authority
            or parsed.execution_allowed
            or parsed.system_verified
            or parsed.user_accepted
        ):
            _fail("agent_untrusted_model_output_invalid")
        if parsed.injection_indicators:
            _fail("agent_prompt_injection_detected")
        payload = dict(parsed.payload)
        if (
            payload.get("proposal_only") is not True
            or payload.get("authorization_claimed") is not False
            or payload.get("execution_claimed") is not False
            or payload.get("registry_grants_authority") is not False
            or payload.get("execution_allowed") is not False
            or payload.get("registry_decision") != "validated_proposal_only"
        ):
            _fail("agent_model_authority_claim_detected")
        keys = (
            "capability_id",
            "capability_version",
            "capability_digest",
            "typed_arguments",
            "reason",
            "expected_observation",
        )
        if any(key not in payload for key in keys):
            _fail("agent_structured_tool_proposal_invalid")
        return {key: payload[key] for key in keys}

    @staticmethod
    def _semantic_overreach_guard(model_proposal: Mapping[str, Any]) -> None:
        capability_id = model_proposal.get("capability_id")
        if capability_id in {
            "git.merge",
            "git.tag",
            "git.push",
            "shell.execute",
            "network.request",
            "database.write",
        }:
            _fail("agent_overreach_blocked")
        arguments = model_proposal.get("typed_arguments")
        if not isinstance(arguments, Mapping):
            _fail("agent_structured_tool_proposal_invalid")
        inspected = [model_proposal.get("reason", "")]
        inspected.extend(
            value
            for key, value in arguments.items()
            if key in {"relative_path", "content", "replacement_content"}
            and isinstance(value, str)
        )
        normalized = [
            _security_text(value)
            for value in inspected
            if isinstance(value, str)
        ]
        if any(_NEXT_VERSION_PATTERN.search(value) for value in normalized):
            _fail("agent_next_version_creation_blocked")
        if any(_FORBIDDEN_TEXT_PATTERN.search(value) for value in normalized):
            _fail("agent_overreach_blocked")
        if capability_id not in _WRITE_CAPABILITIES:
            return

    @staticmethod
    def _runtime_receipt_digest(execution: Any) -> str:
        return canonical_digest(
            {
                "runtime_model_run_id": execution.runtime_model_run_id,
                "runtime_receipt_identity": execution.runtime_receipt_identity,
                "runtime_artifact_identity": execution.runtime_artifact_identity,
            }
        )

    @staticmethod
    def _durable_model_send_count(checkpoint: Any) -> int:
        """Count only model sends proven by a settled Runtime receipt."""

        return sum(
            1
            for binding in checkpoint.runtime_recovery_bindings
            if binding.get("kind") == "model_call"
            and isinstance(binding.get("runtime_execution_reference"), str)
            and isinstance(binding.get("runtime_receipt_identity"), str)
            and isinstance(binding.get("runtime_receipt_digest"), str)
            and isinstance(binding.get("runtime_usage_digest"), str)
        )

    def _quarantine_reserved_model_call(
        self,
        *,
        run_id: str,
        reservation_event_id: str,
    ) -> CognitiveRun:
        latest = self.execution_store.get_run(run_id)
        reconciliation_authority = (
            self.execution_store.open_model_reconciliation_authority(
                reservation_event_id=reservation_event_id,
                lease_owner_id=self.lease_owner_id,
                lease_token=self.lease_token,
            )
        )
        quarantined = self.execution_store.quarantine_model_reservation(
            reconciliation_authority=reconciliation_authority,
            expected_run_digest=latest.run_digest,
            lease_owner_id=self.lease_owner_id,
            lease_token=self.lease_token,
        )
        if (
            quarantined.get("status") != "quarantined"
            or quarantined.get("provider_call_allowed") is not False
        ):
            _fail("agent_model_quarantine_failed")
        return quarantined["run"]

    def _execute_model_turn(
        self,
        *,
        run: CognitiveRun,
        plan: PlanGraph,
        node: PlanNode,
        authorization: CognitiveAuthorizationArtifact,
        budget_state: ModelBudgetState,
        prepared_turn: PreparedRuntimeBridgeTurn | None = None,
    ) -> tuple[CognitiveRun, ParsedModelOutput, str, str]:
        prepared = prepared_turn
        if prepared is None:
            prepared = self.model_turn_source.prepare_turn(
                run=run,
                plan=plan,
                node=node,
                authorization=authorization,
                budget_state=budget_state,
            )
        if not isinstance(prepared, PreparedRuntimeBridgeTurn):
            _fail("agent_prepared_model_turn_required")
        request = prepared.request
        reservation = prepared.reservation
        objective = dict(request.objective)
        if (
            objective.get("objective_id") != run.objective_id
            or objective.get("objective_digest") != run.objective_digest
            or objective.get("agency_level") != "act"
            or request.provider_binding.provider_id != run.provider_id
            or request.provider_binding.profile_version != run.provider_profile_version
            or request.provider_binding.profile_digest != run.provider_profile_digest
            or reservation.sequence != budget_state.calls_reserved + 1
        ):
            _fail("agent_model_turn_run_binding_mismatch")
        reserved = self.execution_store.reserve_model_call(
            run_id=run.run_id,
            expected_run_digest=run.run_digest,
            authorization=authorization,
            reservation=reservation,
            provider_id=run.provider_id,
            provider_profile_version=run.provider_profile_version,
            provider_profile_digest=run.provider_profile_digest,
            request_digest=request.request_digest,
            source_authority=self.source_authority,
            lease_owner_id=self.lease_owner_id,
            lease_token=self.lease_token,
            budget=request.model_budget,
        )
        if reserved["status"] != "reserved" or not reserved.get(
            "provider_call_allowed", False
        ):
            _fail("agent_model_call_outcome_unknown")
        persisted_budget = reserved["budget_state"]
        if persisted_budget != prepared.budget_state_after_reservation:
            _fail("agent_model_budget_reservation_drift")
        reserved_run = reserved["run"]
        execution = None
        try:
            selection = request.provider_binding.selection
            disclosure = prepared.bridge.prepare_disclosure(
                selection,
                request,
                reservation,
                request_id=prepared.request_id,
                mutation_id=prepared.mutation_id,
                budget_state=persisted_budget,
            )
            consent = prepared.consent_factory(disclosure)
            preview = prepared.bridge.preview(
                disclosure,
                selection,
                request,
                reservation,
                consent=consent,
                budget_state=persisted_budget,
            )
            execution = prepared.bridge.execute(
                preview,
                selection,
                request,
                reservation,
                consent=consent,
                confirmed=True,
                authority=prepared.runtime_authority,
                budget_state=persisted_budget,
            )
            if (
                execution.adapter_call_count_this_request != 1
                or execution.model_call_count_for_run != 1
                or execution.exactly_one_call is not True
                or execution.automatic_retry is not False
                or execution.replayed is not False
            ):
                _fail("agent_runtime_one_send_violation")
            observation = prepared.bridge.read_model_observation(
                preview,
                request,
                reservation,
                budget_state=persisted_budget,
            )
        except Exception:
            try:
                self._quarantine_reserved_model_call(
                    run_id=run.run_id,
                    reservation_event_id=reserved["reservation_event_id"],
                )
            except Exception:
                pass
            raise
        terminal_status = getattr(execution, "status", None)
        durable_terminal = (
            terminal_status in {"succeeded", "failed"}
            and getattr(observation, "status", None) == terminal_status
            and isinstance(getattr(observation, "usage", None), Mapping)
            and execution.runtime_receipt_identity is not None
            and execution.runtime_usage_digest is not None
        )
        if not durable_terminal:
            self._quarantine_reserved_model_call(
                run_id=run.run_id,
                reservation_event_id=reserved["reservation_event_id"],
            )
            _fail("agent_model_call_failed")
        usage = dict(observation.usage)
        latest = self.execution_store.get_run(run.run_id)
        settled = self.execution_store.settle_model_call(
            run_id=run.run_id,
            expected_run_digest=latest.run_digest,
            authorization=authorization,
            reservation=reservation,
            outcome=terminal_status,
            actual_cost_microunits=None,
            runtime_execution_reference=execution.runtime_model_run_id,
            runtime_receipt_identity=execution.runtime_receipt_identity,
            runtime_receipt_digest=self._runtime_receipt_digest(execution),
            runtime_usage_digest=execution.runtime_usage_digest,
            usage=usage,
            reconciliation_only=False,
            source_authority=self.source_authority,
            lease_owner_id=self.lease_owner_id,
            lease_token=self.lease_token,
            budget=request.model_budget,
            actual_provider=request.provider_binding.response_identity(),
            actual_input_tokens=usage["prompt_tokens"],
            actual_output_tokens=usage["completion_tokens"],
        )
        if terminal_status == "failed":
            _fail("agent_model_call_failed")
        try:
            reconciliation = prepared.bridge.reconcile_model_run(
                preview,
                request,
                reservation,
                budget_state=persisted_budget,
                capability_registry=self.tool_gateway.registry,
            )
        except Exception as error:
            _fail(_reason(error))
        if (
            reconciliation.state != "reconciled_succeeded"
            or not isinstance(reconciliation.parsed_output, ParsedModelOutput)
            or reconciliation.grants_authority
            or reconciliation.execution_allowed
            or reconciliation.system_verified
            or reconciliation.user_accepted
        ):
            _fail(
                reconciliation.reason_code
                if isinstance(reconciliation.reason_code, str)
                else "agent_structured_model_output_rejected"
            )
        return (
            settled["run"],
            reconciliation.parsed_output,
            execution.runtime_model_run_id,
            self._runtime_receipt_digest(execution),
        )

    @staticmethod
    def _preview_from_reservation(
        execution_store: CognitiveExecutionStore,
        result: ToolExecutionResult,
    ) -> ToolExecutionPreview:
        with closing(execution_store.operational._connect()) as connection:
            row = connection.execute(
                "SELECT canonical_payload FROM state_events WHERE event_id=?",
                (result.reservation_event_id,),
            ).fetchone()
        if row is None:
            _fail("agent_tool_reservation_missing")
        try:
            payload = json.loads(row["canonical_payload"])
            sealed = payload["sealed_preview"]
            if canonical_json(payload) != row["canonical_payload"] or not isinstance(
                sealed, dict
            ):
                _fail("agent_tool_reservation_corrupt")
            values = dict(sealed)
            if values.pop("protocol_version", None) is None:
                _fail("agent_tool_preview_protocol_missing")
            preview = _create_tool_execution_preview(**values)
        except CognitiveAgentLoopError:
            raise
        except Exception:
            _fail("agent_tool_reservation_corrupt")
        if (
            preview.preview_id != result.preview_id
            or preview.preview_digest != result.preview_digest
        ):
            _fail("agent_tool_preview_binding_mismatch")
        return preview

    def _record_verification(
        self,
        *,
        run: CognitiveRun,
        proposal: ToolProposal,
        preview: ToolExecutionPreview,
        result: ToolExecutionResult,
    ) -> tuple[CognitiveRun, IndependentToolVerification, str]:
        verification = self.independent_verifier.verify(
            proposal=proposal,
            preview=preview,
            result=result,
            verified_at=self._now(),
        )
        recorded = self.execution_store.record_independent_tool_verification(
            verification=verification,
            expected_run_digest=run.run_digest,
            source_authority=self.source_authority,
            lease_owner_id=self.lease_owner_id,
            lease_token=self.lease_token,
        )
        return recorded["run"], verification, recorded["verification_id"]

    def _recover_pending_verification(
        self,
        run: CognitiveRun,
    ) -> tuple[CognitiveRun, str] | None:
        checkpoint = self.execution_store.latest_checkpoint(run.run_id)
        if (
            run.status != "verifying"
            or checkpoint.safe_to_resume
            or "tool_execution_pending_independent_verification"
            not in checkpoint.unsafe_reason_codes
            or not checkpoint.execution_ids
        ):
            return None
        execution_id = checkpoint.execution_ids[-1]
        result = next(
            (
                item
                for item in self.execution_store.list_tool_executions(run.run_id)
                if item.execution_id == execution_id
            ),
            None,
        )
        if result is None:
            _fail("agent_pending_tool_execution_missing")
        proposal = self.execution_store.get_tool_proposal(result.proposal_id)
        preview = self._preview_from_reservation(self.execution_store, result)
        current, _verification, verification_id = self._record_verification(
            run=run,
            proposal=proposal,
            preview=preview,
            result=result,
        )
        return current, verification_id

    def _pending_reservation_event_id(
        self,
        *,
        run_id: str,
        event_type: str,
        settled_ids: Sequence[str] = (),
        required: bool = True,
    ) -> str | None:
        if event_type not in {"model_call_reserved", "tool_execution_reserved"}:
            _fail("agent_recovery_reservation_kind_invalid")
        with closing(self.execution_store.operational._connect()) as connection:
            if event_type == "model_call_reserved":
                rows = connection.execute(
                    "SELECT event_id,canonical_payload FROM state_events "
                    "WHERE event_type='model_call_reserved' "
                    "ORDER BY recorded_at,event_id"
                ).fetchall()
                candidates = []
                for row in rows:
                    try:
                        payload = json.loads(row["canonical_payload"])
                    except (TypeError, json.JSONDecodeError):
                        _fail("agent_model_reservation_corrupt")
                    if canonical_json(payload) != row["canonical_payload"]:
                        _fail("agent_model_reservation_corrupt")
                    if (
                        payload.get("run_id") == run_id
                        and payload.get("reservation_id") not in settled_ids
                    ):
                        candidates.append(row["event_id"])
            else:
                rows = connection.execute(
                    "SELECT e.event_id,e.canonical_payload FROM state_events e "
                    "LEFT JOIN tool_executions t "
                    "ON t.reservation_event_id=e.event_id "
                    "WHERE e.event_type='tool_execution_reserved' "
                    "AND t.execution_id IS NULL "
                    "ORDER BY e.recorded_at,e.event_id"
                ).fetchall()
                candidates = []
                for row in rows:
                    try:
                        payload = json.loads(row["canonical_payload"])
                    except (TypeError, json.JSONDecodeError):
                        _fail("agent_tool_reservation_corrupt")
                    if canonical_json(payload) != row["canonical_payload"]:
                        _fail("agent_tool_reservation_corrupt")
                    if payload.get("run_id") == run_id:
                        candidates.append(row["event_id"])
        if not candidates and not required:
            return None
        if len(candidates) != 1:
            _fail("agent_pending_reservation_not_unique")
        return candidates[0]

    def recover(self, run_id: str) -> AgentLoopOutcome:
        """Dispatch one durable recovery action without resending uncertain work."""

        run = self.execution_store.get_run(run_id)
        checkpoint = self.execution_store.latest_checkpoint(run_id)
        reasons = set(checkpoint.unsafe_reason_codes)
        budget_state = _budget_state(checkpoint.model_budget_state)
        model_reservation_pending = (
            budget_state.calls_reserved
            != len(budget_state.settled_reservation_ids)
        )
        if (
            "model_call_reserved_without_settlement" in reasons
            or model_reservation_pending
        ):
            reservation_event_id = self._pending_reservation_event_id(
                run_id=run_id,
                event_type="model_call_reserved",
                settled_ids=budget_state.settled_reservation_ids,
            )
            reconciliation_authority = (
                self.execution_store.open_model_reconciliation_authority(
                    reservation_event_id=reservation_event_id,
                    lease_owner_id=self.lease_owner_id,
                    lease_token=self.lease_token,
                )
            )
            quarantined = self.execution_store.quarantine_model_reservation(
                reconciliation_authority=reconciliation_authority,
                expected_run_digest=run.run_digest,
                lease_owner_id=self.lease_owner_id,
                lease_token=self.lease_token,
            )
            if (
                quarantined.get("status") != "quarantined"
                or quarantined.get("provider_call_allowed") is not False
            ):
                _fail("agent_model_quarantine_failed")
            cancellation = self._cancellation_outcome_if_requested(run_id)
            if cancellation is not None:
                return cancellation
            return AgentLoopOutcome(
                run=quarantined["run"],
                reason_code="model_call_outcome_unknown",
                proposal_ids=(),
                execution_ids=(),
                verification_ids=(),
                supervision_events=self.execution_store.list_supervision_events(
                    run_id
                ),
                model_sends=0,
            )

        pending_tool_event_id = self._pending_reservation_event_id(
            run_id=run_id,
            event_type="tool_execution_reserved",
            required=False,
        )
        if pending_tool_event_id is not None:
            reconciled = self.execution_store.reconcile_reserved_execution(
                reservation_event_id=pending_tool_event_id,
                decision="mark_recovery_required",
                lease_owner_id=self.lease_owner_id,
                lease_token=self.lease_token,
            )
            cancellation = self._cancellation_outcome_if_requested(run_id)
            if cancellation is not None:
                return cancellation
            return AgentLoopOutcome(
                run=reconciled["run"],
                reason_code="tool_execution_outcome_unknown",
                proposal_ids=(),
                execution_ids=(),
                verification_ids=(),
                supervision_events=self.execution_store.list_supervision_events(
                    run_id
                ),
                model_sends=0,
            )

        cancellation = self._cancellation_outcome_if_requested(run_id)
        if cancellation is not None:
            return cancellation

        if run.authorization_id is None:
            return self._fail_closed(
                run_id=run_id,
                reason_code="agent_run_authorization_missing",
                plan_node_id=None,
                proposal_ids=(),
                execution_ids=(),
                verification_ids=(),
                supervision_events=(),
                model_sends=0,
            )
        try:
            authorization = self.execution_store.get_authorization(
                run.authorization_id
            )
            plan = self._load_plan(self.planning_store, run)
            self._assert_authorization_boundary(
                run,
                plan,
                authorization,
                self._now(),
            )
            limits = self._effective_limits(authorization)
            self._assert_persisted_time_boundary(
                run,
                authorization,
                limits,
                self._now(),
            )
        except Exception as error:
            # Recovery must not consume an expired source authorization merely
            # to record that it is expired.  Preserve the exact durable state.
            return AgentLoopOutcome(
                run=self.execution_store.get_run(run_id),
                reason_code=_reason(error),
                proposal_ids=(),
                execution_ids=(),
                verification_ids=(),
                supervision_events=self.execution_store.list_supervision_events(
                    run_id
                ),
                model_sends=0,
            )

        if "tool_execution_pending_independent_verification" in reasons:
            recovered = self._recover_pending_verification(run)
            if recovered is None:
                _fail("agent_verification_recovery_required")
            current, verification_id = recovered
            cancellation = self._cancellation_outcome_if_requested(
                run_id,
                verification_ids=(verification_id,),
            )
            if cancellation is not None:
                return cancellation
            return AgentLoopOutcome(
                run=current,
                reason_code="agent_independent_verification_recovered",
                proposal_ids=(),
                execution_ids=(),
                verification_ids=(verification_id,),
                supervision_events=self.execution_store.list_supervision_events(
                    run_id
                ),
                model_sends=0,
            )
        return AgentLoopOutcome(
            run=run,
            reason_code="agent_no_recovery_action_required",
            proposal_ids=(),
            execution_ids=(),
            verification_ids=(),
            supervision_events=self.execution_store.list_supervision_events(run_id),
            model_sends=0,
        )

    def _transition(
        self,
        run: CognitiveRun,
        next_status: str,
        *,
        failure_code: str | None = None,
    ) -> CognitiveRun:
        return self.execution_store.transition_run(
            run_id=run.run_id,
            expected_run_digest=run.run_digest,
            next_status=next_status,
            failure_code=failure_code,
            source_authority=self.source_authority,
            lease_owner_id=self.lease_owner_id,
            lease_token=self.lease_token,
        )

    def _persist_supervision(
        self,
        *,
        run: CognitiveRun,
        event_type: str,
        severity: str,
        detail: str,
        evidence: Mapping[str, Any],
        action_taken: tuple[str, ...],
        plan_node_id: str | None = None,
        tool_execution_id: str | None = None,
    ) -> tuple[CognitiveRun, SupervisionEvent]:
        persisted = self.execution_store.list_supervision_events(run.run_id)
        event = create_supervision_event(
            run_id=run.run_id,
            sequence=len(persisted) + 1,
            event_type=event_type,
            severity=severity,
            observed_at=self._now(),
            detail=detail,
            evidence=dict(evidence),
            action_taken=action_taken,
            plan_node_id=plan_node_id,
            tool_execution_id=tool_execution_id,
        )
        recorded = self.execution_store.record_supervision_event(
            event=event,
            expected_run_digest=run.run_digest,
            source_authority=self.source_authority,
            lease_owner_id=self.lease_owner_id,
            lease_token=self.lease_token,
        )
        durable = self.execution_store.list_supervision_events(run.run_id)
        if (
            not durable
            or durable[-1].event_digest != event.event_digest
            or durable[-1].sequence != event.sequence
        ):
            _fail("agent_supervision_persistence_failed")
        return recorded["run"], event

    def _fail_closed(
        self,
        *,
        run_id: str,
        reason_code: str,
        plan_node_id: str | None,
        proposal_ids: Sequence[str],
        execution_ids: Sequence[str],
        verification_ids: Sequence[str],
        supervision_events: Sequence[SupervisionEvent],
        model_sends: int,
    ) -> AgentLoopOutcome:
        run = self.execution_store.get_run(run_id)
        checkpoint = self.execution_store.latest_checkpoint(run_id)
        if reason_code in {
                "agent_overreach_blocked",
                "agent_next_version_creation_blocked",
                "agent_prompt_injection_detected",
                "tool_authorization_capability_denied",
                "tool_path_outside_authorized_write_root",
                "tool_path_outside_authorized_read_root",
        }:
            event_type = "overreach_blocked"
        elif reason_code == "suspected_stall":
            event_type = "suspected_stall"
        elif "budget" in reason_code:
            event_type = "budget_exhausted"
        elif "authorization" in reason_code and "expired" in reason_code:
            event_type = "authorization_expired"
        elif "provider" in reason_code or "model_call_failed" in reason_code:
            event_type = "provider_failure"
        else:
            event_type = "recovery_started"
        try:
            run, event = self._persist_supervision(
                run=run,
                event_type=event_type,
                severity="error",
                detail=reason_code,
                evidence={
                    "agent_loop_version": AGENT_LOOP_VERSION,
                    "run_digest": run.run_digest,
                    "checkpoint_digest": checkpoint.checkpoint_digest,
                    "reason_code": reason_code,
                },
                action_taken=(
                    "fail_closed",
                    "no_automatic_retry",
                    "no_acceptance",
                ),
                plan_node_id=plan_node_id,
                tool_execution_id=(execution_ids[-1] if execution_ids else None),
            )
        except Exception:
            # A missing supervision receipt is itself a recovery boundary; it
            # must never be mistaken for a normally failed, resumable run.
            reason_code = "agent_supervision_persistence_failed"
            run = self.execution_store.get_run(run_id)
            event = None
        events = (
            tuple(supervision_events)
            if event is None
            else (*supervision_events, event)
        )
        target = (
            "recovery_required"
            if (
                event is None
                or not checkpoint.safe_to_resume
                or checkpoint.unsafe_reason_codes
                or run.status in {"verifying", "recovery_required"}
            )
            else "failed"
        )
        if run.status not in {"completed", "failed", "cancelled"}:
            try:
                run = self._transition(run, target, failure_code=reason_code)
            except Exception:
                run = self.execution_store.get_run(run_id)
        return AgentLoopOutcome(
            run=run,
            reason_code=reason_code,
            proposal_ids=tuple(proposal_ids),
            execution_ids=tuple(execution_ids),
            verification_ids=tuple(verification_ids),
            supervision_events=tuple(events),
            model_sends=model_sends,
        )

    def _cancellation_outcome_if_requested(
        self,
        run_id: str,
        *,
        proposal_ids: Sequence[str] = (),
        execution_ids: Sequence[str] = (),
        verification_ids: Sequence[str] = (),
        supervision_events: Sequence[SupervisionEvent] = (),
        model_sends: int = 0,
    ) -> AgentLoopOutcome | None:
        artifact = self.execution_store.get_cancellation_request(run_id)
        if artifact is None:
            return None
        run = self.execution_store.get_run(run_id)
        if run.status not in {"cancelled", "recovery_required"}:
            _fail("agent_cancellation_latch_state_invalid")
        return AgentLoopOutcome(
            run=run,
            reason_code=(
                "user_requested_cancellation"
                if run.status == "cancelled"
                else "cancellation_recovery_required"
            ),
            proposal_ids=tuple(proposal_ids),
            execution_ids=tuple(execution_ids),
            verification_ids=tuple(verification_ids),
            supervision_events=(
                tuple(supervision_events)
                or self.execution_store.list_supervision_events(run_id)
            ),
            model_sends=model_sends,
        )

    def _find_pending_equivalent_retry(
        self,
        *,
        run_id: str,
        node: PlanNode,
    ) -> tuple[ToolProposal, ToolExecutionResult] | None:
        executions = self.execution_store.list_tool_executions(run_id)
        attempted_twice = {
            result.proposal_id
            for result in executions
            if result.attempt_number >= 2
        }
        for result in reversed(executions):
            if result.attempt_number != 1 or result.proposal_id in attempted_twice:
                continue
            proposal = self.execution_store.get_tool_proposal(result.proposal_id)
            if proposal.plan_node_id != node.node_id:
                continue
            retryable_reasons = set(result.reason_codes).intersection(
                node.retry_policy.retryable_failures
            )
            if (
                result.status == "failed"
                and result.verification_status == "failed"
                and "tool_execution_no_owned_change" in result.reason_codes
                and retryable_reasons
                and node.retry_policy.maximum_attempts >= 2
                and node.retry_policy.equivalent_retry_limit >= 1
            ):
                return proposal, result
        return None

    def _find_pending_unexecuted_proposal(
        self,
        *,
        checkpoint: Any,
        node: PlanNode,
    ) -> ToolProposal | None:
        executed_proposal_ids = {
            result.proposal_id
            for result in self.execution_store.list_tool_executions(
                checkpoint.run_id
            )
        }
        candidates = tuple(
            proposal
            for proposal in (
                self.execution_store.get_tool_proposal(proposal_id)
                for proposal_id in checkpoint.proposal_ids
            )
            if proposal.plan_node_id == node.node_id
            and proposal.proposal_id not in executed_proposal_ids
        )
        if len(candidates) > 1:
            _fail("agent_pending_proposal_not_unique")
        if not candidates:
            return None
        candidate = candidates[0]
        with closing(self.execution_store.operational._connect()) as connection:
            row = connection.execute(
                "SELECT proposal_digest,runtime_execution_reference,"
                "runtime_receipt_digest FROM tool_proposals WHERE proposal_id=?",
                (candidate.proposal_id,),
            ).fetchone()
        try:
            if (
                row is None
                or row["proposal_digest"] != candidate.proposal_digest
                or not isinstance(row["runtime_execution_reference"], str)
                or not row["runtime_execution_reference"]
            ):
                _fail("agent_pending_proposal_runtime_receipt_missing")
            validate_sha256(
                row["runtime_receipt_digest"],
                "agent_pending_proposal_runtime_receipt_missing",
            )
        except OperationalStateError:
            _fail("agent_pending_proposal_runtime_receipt_missing")
        durable_runtime_binding = any(
            binding.get("kind") == "model_call"
            and binding.get("runtime_execution_reference")
            == row["runtime_execution_reference"]
            and binding.get("runtime_receipt_digest")
            == row["runtime_receipt_digest"]
            for binding in checkpoint.runtime_recovery_bindings
        )
        if not durable_runtime_binding:
            _fail("agent_pending_proposal_runtime_binding_mismatch")
        return candidate

    def _pending_replan_observation(
        self,
        *,
        run_id: str,
        run: CognitiveRun,
        plan: PlanGraph,
        node: PlanNode,
        authorization: CognitiveAuthorizationArtifact,
        checkpoint: Any,
    ) -> Mapping[str, Any] | None:
        matching_events = tuple(
            event
            for event in self.execution_store.list_supervision_events(run_id)
            if event.event_type == "retry_started"
            and event.detail == "bounded_observation_replan_pending"
            and event.plan_node_id == node.node_id
        )
        matching_proposals = tuple(
            self.execution_store.get_tool_proposal(proposal_id)
            for proposal_id in checkpoint.proposal_ids
            if self.execution_store.get_tool_proposal(proposal_id).plan_node_id
            == node.node_id
        )
        if len(matching_events) > 1 or len(matching_proposals) > 2:
            _fail("agent_replan_budget_exhausted")
        if not matching_events or len(matching_proposals) != 1:
            return None
        evidence = dict(matching_events[0].evidence)
        expected = {
            "objective_digest": run.objective_digest,
            "plan_digest": plan.plan_digest,
            "node_id": node.node_id,
            "node_digest": node.node_digest,
            "authorization_digest": authorization.authority_digest,
            "prior_proposal_id": matching_proposals[0].proposal_id,
        }
        if any(evidence.get(key) != value for key, value in expected.items()):
            _fail("agent_replan_binding_mismatch")
        return evidence

    def _backoff_remaining_ms(
        self,
        result: ToolExecutionResult,
        backoff_ms: int,
    ) -> int:
        elapsed = _elapsed_milliseconds(result.finished_at, self._now())
        return max(0, backoff_ms - elapsed)

    @staticmethod
    def _equivalent_retry_allowed(
        *,
        node: PlanNode,
        result: ToolExecutionResult,
        verification_result: str,
        attempt_number: int,
        maximum_attempts: int,
        run_status: str,
    ) -> bool:
        return (
            attempt_number < maximum_attempts
            and attempt_number == 1
            and run_status == "executing"
            and verification_result == "failed"
            and result.status == "failed"
            and "tool_execution_no_owned_change" in result.reason_codes
            and bool(
                set(result.reason_codes)
                & set(node.retry_policy.retryable_failures)
            )
        )

    @staticmethod
    def _bounded_replan_allowed(
        *,
        run: CognitiveRun,
        checkpoint: Any,
        result: ToolExecutionResult,
        verification_result: str,
    ) -> bool:
        denied_markers = (
            "overreach",
            "secret",
            "authorization",
            "authority",
            "path_",
            "outside_",
            "cancellation",
            "outcome_unknown",
            "effect_unknown",
            "recovery",
        )
        reasons = tuple(_security_text(item) for item in result.reason_codes)
        return (
            run.status == "executing"
            and checkpoint.safe_to_resume
            and not checkpoint.unsafe_reason_codes
            and result.status == "failed"
            and verification_result == "failed"
            and "tool_execution_no_owned_change" in result.reason_codes
            and not any(
                marker in reason
                for reason in reasons
                for marker in denied_markers
            )
        )

    def _waiting_outcome(
        self,
        *,
        run: CognitiveRun,
        reason_code: str,
        proposal_ids: Sequence[str],
        execution_ids: Sequence[str],
        verification_ids: Sequence[str],
        supervision_events: Sequence[SupervisionEvent],
        model_sends: int,
    ) -> AgentLoopOutcome:
        return AgentLoopOutcome(
            run=run,
            reason_code=reason_code,
            proposal_ids=tuple(proposal_ids),
            execution_ids=tuple(execution_ids),
            verification_ids=tuple(verification_ids),
            supervision_events=tuple(supervision_events),
            model_sends=model_sends,
        )

    def request_cancellation(
        self,
        artifact: UserCancellationArtifact,
    ) -> AgentLoopOutcome:
        """Latch one sealed user cancellation; never synthesize Agent authority."""

        if not isinstance(artifact, UserCancellationArtifact):
            _fail("sealed_user_cancellation_artifact_required")
        recorded = self.execution_store.record_cancellation_request(
            artifact=artifact,
            source_authority=self.source_authority,
            lease_owner_id=self.lease_owner_id,
            lease_token=self.lease_token,
        )
        run = recorded["run"]
        outcome = self._cancellation_outcome_if_requested(run.run_id)
        if outcome is None:  # pragma: no cover - Store acknowledgement is durable.
            _fail("agent_cancellation_latch_missing")
        return outcome

    def run(self, run_id: str) -> AgentLoopOutcome:
        """Advance one persisted run to ``waiting_acceptance`` at most once."""

        initial = self.execution_store.get_run(run_id)
        initial_checkpoint = self.execution_store.latest_checkpoint(run_id)
        durable_sends_before = self._durable_model_send_count(initial_checkpoint)
        if initial.status == "completed":
            # A user-accepted terminal run is outside this Agent's authority.
            raise CognitiveAgentLoopError("agent_run_already_completed")
        cancellation = self._cancellation_outcome_if_requested(run_id)
        if cancellation is not None:
            return cancellation
        if initial.status == "waiting_acceptance":
            return AgentLoopOutcome(
                run=initial,
                reason_code=None,
                proposal_ids=(),
                execution_ids=(),
                verification_ids=(),
                supervision_events=self.execution_store.list_supervision_events(
                    run_id
                ),
                model_sends=0,
            )
        if initial.status in {"failed", "cancelled"}:
            return AgentLoopOutcome(
                run=initial,
                reason_code=initial.failure_code or "agent_run_terminal",
                proposal_ids=(),
                execution_ids=(),
                verification_ids=(),
                supervision_events=self.execution_store.list_supervision_events(
                    run_id
                ),
                model_sends=0,
            )
        proposal_ids: list[str] = []
        execution_ids: list[str] = []
        verification_ids: list[str] = []
        supervision_events: list[SupervisionEvent] = []
        model_sends = 0
        current_node_id: str | None = None
        try:
            run = initial
            if run.authorization_id is None:
                _fail("agent_run_authorization_missing")
            authorization = self.execution_store.get_authorization(
                run.authorization_id
            )
            plan = self._load_plan(self.planning_store, run)
            self._assert_authorization_boundary(run, plan, authorization, self._now())
            limits = self._effective_limits(authorization)

            checkpoint = self.execution_store.latest_checkpoint(run_id)
            persistent_wait = (
                run.status == "waiting_external"
                and set(checkpoint.unsafe_reason_codes)
                == {"cognitive_run_waiting_external"}
            )
            if (
                not checkpoint.safe_to_resume
                and run.status != "verifying"
                and not persistent_wait
            ):
                _fail("agent_unsettled_operation_recovery_required")
            if run.status == "verifying":
                cancellation = self._cancellation_outcome_if_requested(run_id)
                if cancellation is not None:
                    return cancellation
                recovered = self._recover_pending_verification(run)
                if recovered is not None:
                    run, verification_id = recovered
                    verification_ids.append(verification_id)
                else:
                    checkpoint = self.execution_store.latest_checkpoint(run_id)
                    if checkpoint.current_node_ids or not checkpoint.safe_to_resume:
                        _fail("agent_verification_recovery_required")
                    cancellation = self._cancellation_outcome_if_requested(run_id)
                    if cancellation is not None:
                        return cancellation
                    run = self._transition(run, "waiting_acceptance")
                    return AgentLoopOutcome(
                        run=run,
                        reason_code=None,
                        proposal_ids=(),
                        execution_ids=(),
                        verification_ids=(),
                        supervision_events=(),
                        model_sends=0,
                    )
            else:
                blocker = self._authority_blocker(
                    plan,
                    checkpoint.completed_node_ids,
                )
                if blocker is not None:
                    if run.status != "waiting_authority":
                        run = self._transition(run, "waiting_authority")
                    return AgentLoopOutcome(
                        run=run,
                        reason_code="agent_waiting_authority",
                        proposal_ids=(),
                        execution_ids=(),
                        verification_ids=(),
                        supervision_events=(),
                        model_sends=0,
                    )
                if run.status not in {"executing", "waiting_external"}:
                    run = self._transition(run, "executing")
            self._assert_persisted_time_boundary(
                run,
                authorization,
                limits,
                self._now(),
            )

            while True:
                cancellation = self._cancellation_outcome_if_requested(
                    run_id,
                    proposal_ids=proposal_ids,
                    execution_ids=execution_ids,
                    verification_ids=verification_ids,
                    supervision_events=supervision_events,
                    model_sends=model_sends,
                )
                if cancellation is not None:
                    return cancellation
                checkpoint = self.execution_store.latest_checkpoint(run_id)
                required = {node.node_id for node in self._required_nodes(plan)}
                if required.issubset(set(checkpoint.completed_node_ids)):
                    if checkpoint.current_node_ids:
                        _fail("agent_completed_plan_has_current_nodes")
                    cancellation = self._cancellation_outcome_if_requested(
                        run_id,
                        proposal_ids=proposal_ids,
                        execution_ids=execution_ids,
                        verification_ids=verification_ids,
                        supervision_events=supervision_events,
                        model_sends=model_sends,
                    )
                    if cancellation is not None:
                        return cancellation
                    run = self._transition(run, "verifying")
                    cancellation = self._cancellation_outcome_if_requested(
                        run_id,
                        proposal_ids=proposal_ids,
                        execution_ids=execution_ids,
                        verification_ids=verification_ids,
                        supervision_events=supervision_events,
                        model_sends=model_sends,
                    )
                    if cancellation is not None:
                        return cancellation
                    run = self._transition(run, "waiting_acceptance")
                    return AgentLoopOutcome(
                        run=run,
                        reason_code=None,
                        proposal_ids=tuple(proposal_ids),
                        execution_ids=tuple(execution_ids),
                        verification_ids=tuple(verification_ids),
                        supervision_events=tuple(supervision_events),
                        model_sends=model_sends,
                    )
                self._assert_persisted_time_boundary(
                    run,
                    authorization,
                    limits,
                    self._now(),
                )
                if run.steps_used >= limits.maximum_steps:
                    _fail("agent_step_budget_exhausted")
                node = self._next_node(plan, checkpoint.completed_node_ids)
                if node is None:
                    _fail("agent_plan_not_executable")
                current_node_id = node.node_id
                pending_retry = self._find_pending_equivalent_retry(
                    run_id=run_id,
                    node=node,
                )
                pending_proposal = self._find_pending_unexecuted_proposal(
                    checkpoint=checkpoint,
                    node=node,
                )
                if pending_retry is not None:
                    proposal, prior_result = pending_retry
                    remaining_backoff_ms = self._backoff_remaining_ms(
                        prior_result,
                        node.retry_policy.backoff_ms,
                    )
                    if remaining_backoff_ms > 0:
                        if run.status != "waiting_external":
                            run = self._transition(run, "waiting_external")
                        return self._waiting_outcome(
                            run=run,
                            reason_code="agent_retry_backoff_active",
                            proposal_ids=(proposal.proposal_id,),
                            execution_ids=execution_ids,
                            verification_ids=verification_ids,
                            supervision_events=supervision_events,
                            model_sends=model_sends,
                        )
                    if run.status == "waiting_external":
                        run = self._transition(run, "executing")
                    proposal_ids.append(proposal.proposal_id)
                    first_attempt = 2
                elif pending_proposal is not None:
                    if run.status == "waiting_external":
                        run = self._transition(run, "executing")
                    proposal = pending_proposal
                    proposal_ids.append(proposal.proposal_id)
                    first_attempt = 1
                else:
                    if run.status == "waiting_external":
                        run = self._transition(run, "executing")
                    if run.model_calls_used >= limits.maximum_model_calls:
                        _fail("agent_model_call_budget_exhausted")
                    budget_state = _budget_state(checkpoint.model_budget_state)
                    replan_observation = self._pending_replan_observation(
                        run_id=run_id,
                        run=run,
                        plan=plan,
                        node=node,
                        authorization=authorization,
                        checkpoint=checkpoint,
                    )
                    prepared_replan = None
                    if replan_observation is not None:
                        prepare_replan = getattr(
                            self.model_turn_source,
                            "prepare_replan_turn",
                            None,
                        )
                        if not callable(prepare_replan):
                            _fail("agent_replan_source_unavailable")
                        prepared_replan = prepare_replan(
                            run=run,
                            plan=plan,
                            node=node,
                            authorization=authorization,
                            budget_state=budget_state,
                            observation=dict(replan_observation),
                        )
                    run, parsed, runtime_reference, runtime_receipt_digest = (
                        self._execute_model_turn(
                            run=run,
                            plan=plan,
                            node=node,
                            authorization=authorization,
                            budget_state=budget_state,
                            prepared_turn=prepared_replan,
                        )
                    )
                    model_sends += 1
                    cancellation = self._cancellation_outcome_if_requested(
                        run_id,
                        proposal_ids=proposal_ids,
                        execution_ids=execution_ids,
                        verification_ids=verification_ids,
                        supervision_events=supervision_events,
                        model_sends=model_sends,
                    )
                    if cancellation is not None:
                        return cancellation
                    model_proposal = self._model_proposal(parsed)
                    self._semantic_overreach_guard(model_proposal)
                    proposal = create_tool_proposal(
                        run_id=run.run_id,
                        plan=plan,
                        authorization=authorization,
                        registry=self.tool_gateway.registry,
                        plan_node_id=node.node_id,
                        model_proposal=model_proposal,
                        proposed_at=self._now(),
                    )
                    self.execution_store.record_tool_proposal(
                        proposal=proposal,
                        source_authority=self.source_authority,
                        lease_owner_id=self.lease_owner_id,
                        lease_token=self.lease_token,
                        runtime_execution_reference=runtime_reference,
                        runtime_receipt_digest=runtime_receipt_digest,
                    )
                    proposal_ids.append(proposal.proposal_id)
                    first_attempt = 1
                maximum_attempts = min(
                    2,
                    node.retry_policy.maximum_attempts,
                    1 + node.retry_policy.equivalent_retry_limit,
                )
                for attempt_number in range(first_attempt, maximum_attempts + 1):
                    preview = self.tool_gateway.preview(
                        proposal=proposal,
                        plan=plan,
                        authorization=authorization,
                        attempt_number=attempt_number,
                    )
                    outcome = self.tool_gateway.execute(
                        preview=preview,
                        proposal=proposal,
                        plan=plan,
                        authorization=authorization,
                        source_authority=self.source_authority,
                        lease_owner_id=self.lease_owner_id,
                        lease_token=self.lease_token,
                    )
                    if outcome.idempotent_replay:
                        _fail("agent_unexpected_tool_execution_replay")
                    execution_ids.append(outcome.result.execution_id)
                    run = self.execution_store.get_run(run_id)
                    cancellation = self._cancellation_outcome_if_requested(
                        run_id,
                        proposal_ids=proposal_ids,
                        execution_ids=execution_ids,
                        verification_ids=verification_ids,
                        supervision_events=supervision_events,
                        model_sends=model_sends,
                    )
                    if cancellation is not None:
                        return cancellation
                    run, verification, verification_id = self._record_verification(
                        run=run,
                        proposal=proposal,
                        preview=preview,
                        result=outcome.result,
                    )
                    verification_ids.append(verification_id)
                    run, checkpoint_event = self._persist_supervision(
                        run=run,
                        event_type="checkpoint",
                        severity=(
                            "info"
                            if verification.result == "passed"
                            else "error"
                        ),
                        detail="independent_verification_recorded",
                        evidence={
                            "agent_loop_version": AGENT_LOOP_VERSION,
                            "attempt_number": attempt_number,
                            "execution_id": outcome.result.execution_id,
                            "execution_digest": outcome.result.execution_digest,
                            "verification_id": verification.verification_id,
                            "verification_digest": verification.verification_digest,
                            "verification_result": verification.result,
                        },
                        action_taken=("independent_verification_recorded",),
                        plan_node_id=node.node_id,
                        tool_execution_id=outcome.result.execution_id,
                    )
                    supervision_events.append(checkpoint_event)
                    if verification.result == "passed" and run.status == "executing":
                        break
                    suspected_stall = (
                        "suspected_stall" in outcome.result.reason_codes
                    )
                    retryable = self._equivalent_retry_allowed(
                        node=node,
                        result=outcome.result,
                        verification_result=verification.result,
                        attempt_number=attempt_number,
                        maximum_attempts=maximum_attempts,
                        run_status=run.status,
                    )
                    if suspected_stall:
                        run, stall_event = self._persist_supervision(
                            run=run,
                            event_type="suspected_stall",
                            severity="warning" if retryable else "error",
                            detail="suspected_stall_after_no_progress_check",
                            evidence={
                                "agent_loop_version": AGENT_LOOP_VERSION,
                                "attempt_number": attempt_number,
                                "execution_id": outcome.result.execution_id,
                                "execution_digest": outcome.result.execution_digest,
                                "no_owned_change": (
                                    "tool_execution_no_owned_change"
                                    in outcome.result.reason_codes
                                ),
                                "equivalent_retry_authorized": retryable,
                            },
                            action_taken=(
                                "preserve_stall_evidence",
                                "terminate_owned_process",
                                (
                                    "allow_one_equivalent_retry"
                                    if retryable
                                    else "fail_closed"
                                ),
                            ),
                            plan_node_id=node.node_id,
                            tool_execution_id=outcome.result.execution_id,
                        )
                        supervision_events.append(stall_event)
                        if not retryable:
                            _fail("suspected_stall")
                    if not retryable:
                        existing_replans = tuple(
                            event
                            for event in self.execution_store.list_supervision_events(
                                run_id
                            )
                            if event.event_type == "retry_started"
                            and event.detail == "bounded_observation_replan_pending"
                            and event.plan_node_id == node.node_id
                        )
                        prepare_replan = getattr(
                            self.model_turn_source,
                            "prepare_replan_turn",
                            None,
                        )
                        latest_for_replan = (
                            self.execution_store.latest_checkpoint(run_id)
                        )
                        if (
                            callable(prepare_replan)
                            and not existing_replans
                            and run.model_calls_used < limits.maximum_model_calls
                            and self._bounded_replan_allowed(
                                run=run,
                                checkpoint=latest_for_replan,
                                result=outcome.result,
                                verification_result=verification.result,
                            )
                        ):
                            run, replan_event = self._persist_supervision(
                                run=run,
                                event_type="retry_started",
                                severity="warning",
                                detail="bounded_observation_replan_pending",
                                evidence={
                                    "agent_loop_version": AGENT_LOOP_VERSION,
                                    "objective_digest": run.objective_digest,
                                    "plan_digest": plan.plan_digest,
                                    "node_id": node.node_id,
                                    "node_digest": node.node_digest,
                                    "authorization_digest": (
                                        authorization.authority_digest
                                    ),
                                    "prior_proposal_id": proposal.proposal_id,
                                    "prior_proposal_digest": (
                                        proposal.proposal_digest
                                    ),
                                    "prior_execution_id": (
                                        outcome.result.execution_id
                                    ),
                                    "prior_execution_digest": (
                                        outcome.result.execution_digest
                                    ),
                                    "verification_id": (
                                        verification.verification_id
                                    ),
                                    "verification_digest": (
                                        verification.verification_digest
                                    ),
                                    "verification_result": verification.result,
                                },
                                action_taken=(
                                    "persist_bounded_observation",
                                    "same_objective_plan_node_authority_only",
                                    "reserve_at_most_one_additional_model_call",
                                    "no_acceptance",
                                ),
                                plan_node_id=node.node_id,
                                tool_execution_id=outcome.result.execution_id,
                            )
                            supervision_events.append(replan_event)
                            run = self._transition(run, "waiting_external")
                            return self._waiting_outcome(
                                run=run,
                                reason_code="agent_replan_pending",
                                proposal_ids=proposal_ids,
                                execution_ids=execution_ids,
                                verification_ids=verification_ids,
                                supervision_events=supervision_events,
                                model_sends=model_sends,
                            )
                        _fail("agent_independent_verification_failed")
                    run, retry_event = self._persist_supervision(
                        run=run,
                        event_type="retry_started",
                        severity="warning",
                        detail="equivalent_retry_started",
                        evidence={
                            "agent_loop_version": AGENT_LOOP_VERSION,
                            "prior_execution_id": outcome.result.execution_id,
                            "prior_execution_digest": outcome.result.execution_digest,
                            "retry_attempt_number": attempt_number + 1,
                            "retryable_reason_codes": sorted(
                                set(outcome.result.reason_codes)
                                & set(node.retry_policy.retryable_failures)
                            ),
                        },
                        action_taken=(
                            "one_equivalent_retry",
                            "reuse_same_tool_proposal",
                            "no_additional_model_call",
                        ),
                        plan_node_id=node.node_id,
                        tool_execution_id=outcome.result.execution_id,
                    )
                    supervision_events.append(retry_event)
                    if node.retry_policy.backoff_ms > 0:
                        run = self._transition(run, "waiting_external")
                        return self._waiting_outcome(
                            run=run,
                            reason_code="agent_retry_backoff_active",
                            proposal_ids=proposal_ids,
                            execution_ids=execution_ids,
                            verification_ids=verification_ids,
                            supervision_events=supervision_events,
                            model_sends=model_sends,
                        )
                else:  # pragma: no cover - the bounded loop always breaks or fails.
                    _fail("agent_retry_budget_exhausted")
        except Exception as error:
            try:
                durable_sends_after = self._durable_model_send_count(
                    self.execution_store.latest_checkpoint(run_id)
                )
                model_sends = max(
                    model_sends,
                    max(0, durable_sends_after - durable_sends_before),
                )
            except Exception:
                # Preserve the primary fail-closed reason if checkpoint
                # telemetry itself cannot be read.
                pass
            return self._fail_closed(
                run_id=run_id,
                reason_code=_reason(error),
                plan_node_id=current_node_id,
                proposal_ids=proposal_ids,
                execution_ids=execution_ids,
                verification_ids=verification_ids,
                supervision_events=supervision_events,
                model_sends=model_sends,
            )


__all__ = [
    "AGENT_LOOP_VERSION",
    "AgentLoopLimits",
    "AgentLoopOutcome",
    "BoundedCognitiveAgentLoop",
    "CognitiveAgentLoopError",
    "PreparedRuntimeBridgeTurn",
    "RuntimeBridgeTurnSource",
]
