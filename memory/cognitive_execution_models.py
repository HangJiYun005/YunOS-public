from __future__ import annotations

import json
from dataclasses import InitVar, dataclass
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from memory.cognitive_authorization import CognitiveAuthorizationArtifact
from memory.cognitive_capability import CapabilityProposal, CapabilityRegistry
from memory.cognitive_orchestration import contains_secret_material
from memory.cognitive_planning import PlanGraph, PlanNode
from memory.operational_state_models import (
    OperationalStateError,
    canonical_digest,
    canonical_json,
    make_identity,
    normalize_timestamp,
    validate_identity,
    validate_sha256,
)


TOOL_PROTOCOL_VERSION = 1
POLICY_DECISIONS = (
    "allow",
    "deny_schema",
    "deny_scope",
    "deny_authority",
    "deny_risk",
    "deny_path",
    "deny_command",
    "deny_secret",
    "deny_budget",
    "deny_replay",
)
EXECUTION_STATUSES = (
    "denied",
    "succeeded",
    "failed",
    "cancelled",
    "recovery_required",
)
VERIFICATION_STATUSES = ("pending", "passed", "failed", "unverifiable")
INVOCATION_KINDS = ("native_call", "fixed_process", "denied")

_PROPOSAL_FACTORY_SEAL = object()
_PREVIEW_FACTORY_SEAL = object()
_EXECUTION_FACTORY_SEAL = object()
_RUN_FACTORY_SEAL = object()
_CHECKPOINT_FACTORY_SEAL = object()
_MAX_TEXT = 4096
_MAX_ARTIFACTS = 64


class CognitiveExecutionModelError(OperationalStateError):
    pass


def _fail(reason_code: str) -> None:
    raise CognitiveExecutionModelError(reason_code)


def _json_copy(value: Any, reason_code: str) -> Any:
    try:
        return json.loads(canonical_json(value))
    except (OperationalStateError, TypeError, ValueError):
        _fail(reason_code)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _normalize_text(value: Any, reason_code: str, *, maximum: int = _MAX_TEXT) -> str:
    if not isinstance(value, str):
        _fail(reason_code)
    normalized = " ".join(value.strip().split())
    if not normalized or len(normalized) > maximum:
        _fail(reason_code)
    if any(ord(char) < 32 for char in normalized):
        _fail(reason_code)
    if contains_secret_material(normalized):
        _fail("tool_proposal_secret_detected")
    return normalized


def _identity(value: Any, reason_code: str) -> str:
    try:
        return validate_identity(value, reason_code)
    except OperationalStateError:
        _fail(reason_code)


def _sha256(value: Any, reason_code: str) -> str:
    try:
        return validate_sha256(value, reason_code)
    except OperationalStateError:
        _fail(reason_code)


def _node_for(plan: PlanGraph, node_id: str) -> PlanNode:
    node = next((item for item in plan.nodes if item.node_id == node_id), None)
    if node is None:
        _fail("tool_proposal_plan_node_missing")
    if node.node_kind in {"human_gate", "decision"}:
        _fail("tool_proposal_control_node_not_executable")
    if node.state not in {"proposed", "ready", "running"}:
        _fail("tool_proposal_plan_node_not_executable")
    return node


@dataclass(frozen=True)
class ToolProposal:
    proposal_id: str
    run_id: str
    objective_id: str
    objective_digest: str
    plan_id: str
    plan_version: int
    plan_digest: str
    plan_node_id: str
    capability_id: str
    capability_version: int
    capability_digest: str
    typed_arguments: Mapping[str, Any]
    argument_digest: str
    idempotency_key: str
    reason: str
    expected_observation: str
    risk_class: str
    authorization_id: str
    authorization_digest: str
    status: str
    proposed_at: str
    runtime_execution_reference: str | None
    runtime_receipt_digest: str | None
    proposal_digest: str = ""
    _factory_seal: InitVar[object | None] = None

    def __post_init__(self, _factory_seal: object | None) -> None:
        if _factory_seal is not _PROPOSAL_FACTORY_SEAL:
            _fail("tool_proposal_factory_required")
        for value, code in (
            (self.proposal_id, "invalid_tool_proposal_id"),
            (self.run_id, "invalid_tool_run_id"),
            (self.objective_id, "invalid_tool_objective_id"),
            (self.plan_id, "invalid_tool_plan_id"),
            (self.plan_node_id, "invalid_tool_plan_node_id"),
            (self.capability_id, "invalid_tool_capability_id"),
            (self.idempotency_key, "invalid_tool_idempotency_key"),
            (self.authorization_id, "invalid_tool_authorization_id"),
        ):
            _identity(value, code)
        for value, code in (
            (self.objective_digest, "invalid_tool_objective_digest"),
            (self.plan_digest, "invalid_tool_plan_digest"),
            (self.capability_digest, "invalid_tool_capability_digest"),
            (self.argument_digest, "invalid_tool_argument_digest"),
            (self.authorization_digest, "invalid_tool_authorization_digest"),
        ):
            _sha256(value, code)
        if isinstance(self.plan_version, bool) or not isinstance(self.plan_version, int) or self.plan_version < 1:
            _fail("invalid_tool_plan_version")
        if isinstance(self.capability_version, bool) or not isinstance(self.capability_version, int) or self.capability_version < 1:
            _fail("invalid_tool_capability_version")
        arguments = _json_copy(self.typed_arguments, "invalid_tool_typed_arguments")
        if not isinstance(arguments, dict):
            _fail("invalid_tool_typed_arguments")
        if canonical_digest(arguments) != self.argument_digest:
            _fail("tool_argument_digest_mismatch")
        object.__setattr__(self, "typed_arguments", _freeze(arguments))
        object.__setattr__(self, "reason", _normalize_text(self.reason, "invalid_tool_reason"))
        object.__setattr__(
            self,
            "expected_observation",
            _normalize_text(
                self.expected_observation,
                "invalid_tool_expected_observation",
            ),
        )
        if self.risk_class not in {"low", "medium", "high", "critical"}:
            _fail("invalid_tool_risk_class")
        if self.status != "proposed":
            _fail("tool_proposal_factory_status_required")
        object.__setattr__(self, "proposed_at", normalize_timestamp(self.proposed_at))
        if self.runtime_execution_reference is not None and (
            not isinstance(self.runtime_execution_reference, str)
            or not self.runtime_execution_reference
            or len(self.runtime_execution_reference) > 512
        ):
            _fail("invalid_tool_runtime_execution_reference")
        if self.runtime_receipt_digest is not None:
            _sha256(
                self.runtime_receipt_digest,
                "invalid_tool_runtime_receipt_digest",
            )
        expected = canonical_digest(self.identity_payload())
        if self.proposal_digest and self.proposal_digest != expected:
            _fail("tool_proposal_digest_mismatch")
        object.__setattr__(self, "proposal_digest", expected)

    def identity_payload(self) -> dict[str, Any]:
        return {
            "protocol_version": TOOL_PROTOCOL_VERSION,
            "proposal_id": self.proposal_id,
            "run_id": self.run_id,
            "objective_id": self.objective_id,
            "objective_digest": self.objective_digest,
            "plan_id": self.plan_id,
            "plan_version": self.plan_version,
            "plan_digest": self.plan_digest,
            "plan_node_id": self.plan_node_id,
            "capability_id": self.capability_id,
            "capability_version": self.capability_version,
            "capability_digest": self.capability_digest,
            "typed_arguments": _thaw(self.typed_arguments),
            "argument_digest": self.argument_digest,
            "idempotency_key": self.idempotency_key,
            "reason": self.reason,
            "expected_observation": self.expected_observation,
            "risk_class": self.risk_class,
            "authorization_id": self.authorization_id,
            "authorization_digest": self.authorization_digest,
            "status": self.status,
            "proposed_at": self.proposed_at,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.identity_payload(),
            "runtime_execution_reference": self.runtime_execution_reference,
            "runtime_receipt_digest": self.runtime_receipt_digest,
            "proposal_digest": self.proposal_digest,
        }


def create_tool_proposal(
    *,
    run_id: str,
    plan: PlanGraph,
    authorization: CognitiveAuthorizationArtifact,
    registry: CapabilityRegistry,
    plan_node_id: str,
    model_proposal: Mapping[str, Any],
    proposed_at: str,
) -> ToolProposal:
    if not isinstance(plan, PlanGraph):
        _fail("invalid_tool_plan")
    if not isinstance(authorization, CognitiveAuthorizationArtifact):
        _fail("invalid_tool_authorization")
    if not isinstance(registry, CapabilityRegistry):
        _fail("invalid_tool_capability_registry")
    _identity(run_id, "invalid_tool_run_id")
    timestamp = normalize_timestamp(proposed_at)
    authorization.assert_current(timestamp)
    if (
        authorization.plan_id != plan.plan_id
        or authorization.plan_version != plan.version
        or authorization.plan_digest != plan.plan_digest
        or authorization.objective_id != plan.objective_id
        or authorization.objective_digest != plan.objective_digest
    ):
        _fail("tool_authorization_plan_binding_mismatch")
    node = _node_for(plan, _identity(plan_node_id, "invalid_tool_plan_node_id"))
    expected_keys = {
        "capability_id",
        "capability_version",
        "capability_digest",
        "typed_arguments",
        "reason",
        "expected_observation",
    }
    if not isinstance(model_proposal, Mapping) or set(model_proposal) != expected_keys:
        _fail("tool_proposal_shape_invalid")
    profile = registry.lookup(
        model_proposal["capability_id"],
        version=model_proposal["capability_version"],
        expected_digest=model_proposal["capability_digest"],
    )
    risk_order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    if risk_order[profile.risk_class] > risk_order[authorization.risk_ceiling]:
        _fail("tool_authorization_risk_ceiling_exceeded")
    if profile.availability != "available":
        _fail(f"tool_capability_{profile.availability}")
    required = any(
        item.capability_id == profile.capability_id
        and item.capability_version == profile.version
        and item.capability_digest == profile.capability_digest
        for item in node.capability_requirements
    )
    if not required:
        _fail("tool_capability_not_required_by_plan_node")
    if not authorization.allows_capability(
        profile.capability_id,
        profile.version,
        profile.capability_digest,
    ):
        _fail("tool_authorization_capability_denied")
    arguments = profile.validate_input(model_proposal["typed_arguments"])
    binding = make_identity(
        "toolbinding",
        {
            "run_id": run_id,
            "objective_digest": plan.objective_digest,
            "plan_digest": plan.plan_digest,
            "node_digest": node.node_digest,
            "authorization_digest": authorization.authority_digest,
        },
    )
    capability_proposal = CapabilityProposal.create(
        profile=profile,
        typed_arguments=arguments,
        proposal_binding=binding,
    )
    proposal_identity = {
        "run_id": run_id,
        "plan_node_id": node.node_id,
        "capability_id": profile.capability_id,
        "capability_digest": profile.capability_digest,
        "argument_digest": capability_proposal.argument_digest,
        "authorization_digest": authorization.authority_digest,
        "idempotency_key": capability_proposal.idempotency_key,
    }
    return ToolProposal(
        proposal_id=make_identity("toolproposal", proposal_identity),
        run_id=run_id,
        objective_id=plan.objective_id,
        objective_digest=plan.objective_digest,
        plan_id=plan.plan_id,
        plan_version=plan.version,
        plan_digest=plan.plan_digest,
        plan_node_id=node.node_id,
        capability_id=profile.capability_id,
        capability_version=profile.version,
        capability_digest=profile.capability_digest,
        typed_arguments=arguments,
        argument_digest=capability_proposal.argument_digest,
        idempotency_key=capability_proposal.idempotency_key,
        reason=model_proposal["reason"],
        expected_observation=model_proposal["expected_observation"],
        risk_class=profile.risk_class,
        authorization_id=authorization.authorization_id,
        authorization_digest=authorization.authority_digest,
        status="proposed",
        proposed_at=timestamp,
        runtime_execution_reference=None,
        runtime_receipt_digest=None,
        _factory_seal=_PROPOSAL_FACTORY_SEAL,
    )


@dataclass(frozen=True)
class ToolExecutionPreview:
    preview_id: str
    proposal_digest: str
    authorization_digest: str
    plan_digest: str
    node_digest: str
    capability_digest: str
    attempt_number: int
    invocation_kind: str
    resolved_argv: tuple[str, ...]
    working_directory: str
    resolved_bindings: tuple[Mapping[str, Any], ...]
    template_id: str | None
    template_digest: str | None
    effective_timeout_ms: int
    effective_output_bytes: int
    prepared_at: str
    expires_at: str
    preview_digest: str = ""
    _factory_seal: InitVar[object | None] = None

    def __post_init__(self, _factory_seal: object | None) -> None:
        if _factory_seal is not _PREVIEW_FACTORY_SEAL:
            _fail("tool_execution_preview_factory_required")
        _identity(self.preview_id, "invalid_tool_preview_id")
        for value, code in (
            (self.proposal_digest, "invalid_tool_preview_proposal_digest"),
            (self.authorization_digest, "invalid_tool_preview_authorization_digest"),
            (self.plan_digest, "invalid_tool_preview_plan_digest"),
            (self.node_digest, "invalid_tool_preview_node_digest"),
            (self.capability_digest, "invalid_tool_preview_capability_digest"),
        ):
            _sha256(value, code)
        if (
            isinstance(self.attempt_number, bool)
            or not isinstance(self.attempt_number, int)
            or self.attempt_number not in {1, 2}
        ):
            _fail("invalid_tool_preview_attempt")
        if self.invocation_kind not in {"native_call", "fixed_process"}:
            _fail("invalid_tool_preview_invocation_kind")
        if isinstance(self.resolved_argv, (str, bytes)) or not isinstance(self.resolved_argv, Sequence):
            _fail("invalid_tool_preview_argv")
        argv = tuple(self.resolved_argv)
        if any(not isinstance(item, str) or not item or len(item) > 4096 for item in argv):
            _fail("invalid_tool_preview_argv")
        if self.invocation_kind == "native_call" and argv:
            _fail("native_tool_preview_argv_must_be_empty")
        if self.invocation_kind == "fixed_process" and not argv:
            _fail("process_tool_preview_argv_required")
        object.__setattr__(self, "resolved_argv", argv)
        if not isinstance(self.working_directory, str) or not self.working_directory:
            _fail("invalid_tool_preview_working_directory")
        bindings = _json_copy(list(self.resolved_bindings), "invalid_tool_preview_bindings")
        if not isinstance(bindings, list) or len(bindings) > 256 or any(
            not isinstance(item, dict) for item in bindings
        ):
            _fail("invalid_tool_preview_bindings")
        object.__setattr__(self, "resolved_bindings", tuple(_freeze(item) for item in bindings))
        if (self.template_id is None) != (self.template_digest is None):
            _fail("incomplete_tool_preview_template")
        if self.template_id is not None:
            _identity(self.template_id, "invalid_tool_preview_template_id")
            _sha256(self.template_digest, "invalid_tool_preview_template_digest")
        for value, code, maximum in (
            (self.effective_timeout_ms, "invalid_tool_preview_timeout", 1_200_000),
            (self.effective_output_bytes, "invalid_tool_preview_output_limit", 104_857_600),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                _fail(code)
        object.__setattr__(self, "prepared_at", normalize_timestamp(self.prepared_at))
        object.__setattr__(self, "expires_at", normalize_timestamp(self.expires_at))
        if self.expires_at <= self.prepared_at:
            _fail("invalid_tool_preview_interval")
        expected = canonical_digest(self.identity_payload())
        if self.preview_digest and self.preview_digest != expected:
            _fail("tool_preview_digest_mismatch")
        object.__setattr__(self, "preview_digest", expected)

    def identity_payload(self) -> dict[str, Any]:
        return {
            "protocol_version": TOOL_PROTOCOL_VERSION,
            "preview_id": self.preview_id,
            "proposal_digest": self.proposal_digest,
            "authorization_digest": self.authorization_digest,
            "plan_digest": self.plan_digest,
            "node_digest": self.node_digest,
            "capability_digest": self.capability_digest,
            "attempt_number": self.attempt_number,
            "invocation_kind": self.invocation_kind,
            "resolved_argv": list(self.resolved_argv),
            "working_directory": self.working_directory,
            "resolved_bindings": [_thaw(item) for item in self.resolved_bindings],
            "template_id": self.template_id,
            "template_digest": self.template_digest,
            "effective_timeout_ms": self.effective_timeout_ms,
            "effective_output_bytes": self.effective_output_bytes,
            "prepared_at": self.prepared_at,
            "expires_at": self.expires_at,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_payload(), "preview_digest": self.preview_digest}


def _create_tool_execution_preview(**values: Any) -> ToolExecutionPreview:
    return ToolExecutionPreview(**values, _factory_seal=_PREVIEW_FACTORY_SEAL)


@dataclass(frozen=True)
class ToolExecutionResult:
    execution_id: str
    proposal_id: str
    proposal_digest: str
    run_id: str
    idempotency_key: str
    reservation_event_id: str | None
    reservation_receipt_digest: str | None
    preview_id: str | None
    preview_digest: str | None
    attempt_number: int
    invocation_kind: str
    executor_kind: str
    resolved_argv: tuple[str, ...]
    working_directory: str | None
    policy_decision: str
    reason_codes: tuple[str, ...]
    started_at: str | None
    finished_at: str
    status: str
    exit_code: int | None
    output: Mapping[str, Any]
    stdout_digest: str | None
    stderr_digest: str | None
    artifact_references: tuple[str, ...]
    runtime_execution_reference: str | None
    runtime_receipt_digest: str | None
    verification_status: str
    execution_digest: str = ""
    _factory_seal: InitVar[object | None] = None

    def __post_init__(self, _factory_seal: object | None) -> None:
        if _factory_seal is not _EXECUTION_FACTORY_SEAL:
            _fail("tool_execution_result_factory_required")
        for value, code in (
            (self.execution_id, "invalid_tool_execution_id"),
            (self.proposal_id, "invalid_tool_execution_proposal_id"),
            (self.run_id, "invalid_tool_execution_run_id"),
            (self.idempotency_key, "invalid_tool_execution_idempotency_key"),
        ):
            _identity(value, code)
        _sha256(self.proposal_digest, "invalid_tool_execution_proposal_digest")
        if (self.reservation_event_id is None) != (
            self.reservation_receipt_digest is None
        ):
            _fail("incomplete_tool_execution_reservation")
        if (self.preview_id is None) != (self.preview_digest is None):
            _fail("incomplete_tool_execution_preview_binding")
        if self.reservation_event_id is not None:
            _identity(self.reservation_event_id, "invalid_tool_reservation_event_id")
            _sha256(
                self.reservation_receipt_digest,
                "invalid_tool_reservation_receipt_digest",
            )
        if self.preview_id is not None:
            _identity(self.preview_id, "invalid_tool_execution_preview_id")
            _sha256(
                self.preview_digest,
                "invalid_tool_execution_preview_digest",
            )
        if self.attempt_number not in {1, 2}:
            _fail("invalid_tool_execution_attempt")
        if self.invocation_kind not in INVOCATION_KINDS:
            _fail("invalid_tool_execution_invocation_kind")
        if self.executor_kind not in {
            "native",
            "runtime",
            "codex",
            "external_agent",
            "local_broker",
        }:
            _fail("invalid_tool_execution_executor_kind")
        argv = tuple(self.resolved_argv)
        if any(not isinstance(item, str) or not item or len(item) > 4096 for item in argv):
            _fail("invalid_tool_execution_argv")
        object.__setattr__(self, "resolved_argv", argv)
        if self.policy_decision not in POLICY_DECISIONS:
            _fail("invalid_tool_policy_decision")
        reasons = tuple(sorted(set(self.reason_codes)))
        if not reasons or any(
            not isinstance(item, str) or not item or len(item) > 128
            for item in reasons
        ):
            _fail("invalid_tool_execution_reason_codes")
        object.__setattr__(self, "reason_codes", reasons)
        if self.status not in EXECUTION_STATUSES:
            _fail("invalid_tool_execution_status")
        if self.policy_decision == "allow":
            if (
                self.reservation_event_id is None
                or self.preview_id is None
                or self.invocation_kind == "denied"
                or not self.working_directory
                or self.started_at is None
            ):
                _fail("invalid_allowed_tool_execution")
            if self.invocation_kind == "native_call" and argv:
                _fail("native_tool_execution_argv_must_be_empty")
            if self.invocation_kind == "fixed_process" and not argv:
                _fail("process_tool_execution_argv_required")
            if self.status == "denied":
                _fail("allowed_tool_execution_cannot_be_denied")
        else:
            if (
                self.invocation_kind != "denied"
                or argv
                or self.working_directory is not None
                or self.started_at is not None
                or self.exit_code is not None
                or self.status != "denied"
                or self.reservation_event_id is not None
                or self.preview_id is not None
            ):
                _fail("invalid_denied_tool_execution")
        if self.started_at is not None:
            object.__setattr__(self, "started_at", normalize_timestamp(self.started_at))
        object.__setattr__(self, "finished_at", normalize_timestamp(self.finished_at))
        if self.started_at is not None and self.finished_at < self.started_at:
            _fail("invalid_tool_execution_interval")
        normalized_output = _json_copy(self.output, "invalid_tool_execution_output")
        if not isinstance(normalized_output, dict):
            _fail("invalid_tool_execution_output")
        object.__setattr__(self, "output", _freeze(normalized_output))
        for value, code in (
            (self.stdout_digest, "invalid_tool_stdout_digest"),
            (self.stderr_digest, "invalid_tool_stderr_digest"),
            (self.runtime_receipt_digest, "invalid_tool_runtime_receipt_digest"),
        ):
            if value is not None:
                _sha256(value, code)
        artifacts = tuple(sorted(set(self.artifact_references)))
        if len(artifacts) > _MAX_ARTIFACTS or any(
            not isinstance(item, str) or not item or len(item) > 1024
            for item in artifacts
        ):
            _fail("invalid_tool_artifact_references")
        object.__setattr__(self, "artifact_references", artifacts)
        if self.runtime_execution_reference is not None and (
            not isinstance(self.runtime_execution_reference, str)
            or not self.runtime_execution_reference
            or len(self.runtime_execution_reference) > 512
        ):
            _fail("invalid_tool_runtime_execution_reference")
        if self.verification_status not in VERIFICATION_STATUSES:
            _fail("invalid_tool_verification_status")
        expected = canonical_digest(self.identity_payload())
        if self.execution_digest and self.execution_digest != expected:
            _fail("tool_execution_digest_mismatch")
        object.__setattr__(self, "execution_digest", expected)

    def identity_payload(self) -> dict[str, Any]:
        return {
            "protocol_version": TOOL_PROTOCOL_VERSION,
            "execution_id": self.execution_id,
            "proposal_id": self.proposal_id,
            "proposal_digest": self.proposal_digest,
            "run_id": self.run_id,
            "idempotency_key": self.idempotency_key,
            "reservation_event_id": self.reservation_event_id,
            "reservation_receipt_digest": self.reservation_receipt_digest,
            "preview_id": self.preview_id,
            "preview_digest": self.preview_digest,
            "attempt_number": self.attempt_number,
            "invocation_kind": self.invocation_kind,
            "executor_kind": self.executor_kind,
            "resolved_argv": list(self.resolved_argv),
            "working_directory": self.working_directory,
            "policy_decision": self.policy_decision,
            "reason_codes": list(self.reason_codes),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "status": self.status,
            "exit_code": self.exit_code,
            "output": _thaw(self.output),
            "stdout_digest": self.stdout_digest,
            "stderr_digest": self.stderr_digest,
            "artifact_references": list(self.artifact_references),
            "runtime_execution_reference": self.runtime_execution_reference,
            "runtime_receipt_digest": self.runtime_receipt_digest,
            "verification_status": self.verification_status,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_payload(), "execution_digest": self.execution_digest}


def _create_tool_execution_result(**values: Any) -> ToolExecutionResult:
    return ToolExecutionResult(**values, _factory_seal=_EXECUTION_FACTORY_SEAL)


def _tool_proposal_from_verified_record(value: Mapping[str, Any]) -> ToolProposal:
    if not isinstance(value, Mapping):
        _fail("invalid_tool_proposal_record")
    payload = dict(value)
    if payload.pop("protocol_version", None) != TOOL_PROTOCOL_VERSION:
        _fail("invalid_tool_proposal_protocol")
    return ToolProposal(**payload, _factory_seal=_PROPOSAL_FACTORY_SEAL)


def _tool_execution_from_verified_record(
    value: Mapping[str, Any],
) -> ToolExecutionResult:
    if not isinstance(value, Mapping):
        _fail("invalid_tool_execution_record")
    payload = dict(value)
    if payload.pop("protocol_version", None) != TOOL_PROTOCOL_VERSION:
        _fail("invalid_tool_execution_protocol")
    for name in ("resolved_argv", "reason_codes", "artifact_references"):
        if not isinstance(payload.get(name), list):
            _fail("invalid_tool_execution_record")
        payload[name] = tuple(payload[name])
    return ToolExecutionResult(**payload, _factory_seal=_EXECUTION_FACTORY_SEAL)


RUN_STATUSES = (
    "queued",
    "reasoning",
    "planning",
    "waiting_authority",
    "executing",
    "waiting_external",
    "verifying",
    "waiting_acceptance",
    "completed",
    "failed",
    "cancelled",
    "recovery_required",
)


@dataclass(frozen=True)
class CognitiveRun:
    run_id: str
    objective_id: str
    objective_version: int
    objective_digest: str
    plan_id: str
    plan_version: int
    plan_digest: str
    provider_id: str
    provider_profile_version: int
    provider_profile_digest: str
    authorization_id: str | None
    authorization_digest: str | None
    status: str
    checkpoint_sequence: int
    last_safe_checkpoint: str | None
    model_calls_used: int
    tokens_used: int
    cost_used_microunits: int
    steps_used: int
    started_at: str | None
    last_activity_at: str | None
    ended_at: str | None
    failure_code: str | None
    run_digest: str = ""
    _factory_seal: InitVar[object | None] = None

    def __post_init__(self, _factory_seal: object | None) -> None:
        if _factory_seal is not _RUN_FACTORY_SEAL:
            _fail("cognitive_run_factory_required")
        for value, code in (
            (self.run_id, "invalid_cognitive_run_id"),
            (self.objective_id, "invalid_cognitive_run_objective_id"),
            (self.plan_id, "invalid_cognitive_run_plan_id"),
            (self.provider_id, "invalid_cognitive_run_provider_id"),
        ):
            _identity(value, code)
        for value, code in (
            (self.objective_digest, "invalid_cognitive_run_objective_digest"),
            (self.plan_digest, "invalid_cognitive_run_plan_digest"),
            (self.provider_profile_digest, "invalid_cognitive_run_provider_digest"),
        ):
            _sha256(value, code)
        if (self.authorization_id is None) != (self.authorization_digest is None):
            _fail("incomplete_cognitive_run_authorization")
        if self.authorization_id is not None:
            _identity(self.authorization_id, "invalid_cognitive_run_authorization_id")
            _sha256(
                self.authorization_digest,
                "invalid_cognitive_run_authorization_digest",
            )
        for value, code in (
            (self.objective_version, "invalid_cognitive_run_objective_version"),
            (self.plan_version, "invalid_cognitive_run_plan_version"),
            (self.provider_profile_version, "invalid_cognitive_run_provider_version"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                _fail(code)
        if self.status not in RUN_STATUSES:
            _fail("invalid_cognitive_run_status")
        for value, code in (
            (self.checkpoint_sequence, "invalid_cognitive_run_checkpoint_sequence"),
            (self.model_calls_used, "invalid_cognitive_run_model_calls"),
            (self.tokens_used, "invalid_cognitive_run_tokens"),
            (self.cost_used_microunits, "invalid_cognitive_run_cost"),
            (self.steps_used, "invalid_cognitive_run_steps"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                _fail(code)
        if self.last_safe_checkpoint is not None:
            _sha256(
                self.last_safe_checkpoint,
                "invalid_cognitive_run_safe_checkpoint",
            )
        for field_name in ("started_at", "last_activity_at", "ended_at"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, normalize_timestamp(value))
        if self.failure_code is not None and (
            not isinstance(self.failure_code, str)
            or not self.failure_code
            or len(self.failure_code) > 128
        ):
            _fail("invalid_cognitive_run_failure_code")
        expected = canonical_digest(self.identity_payload())
        if self.run_digest and self.run_digest != expected:
            _fail("cognitive_run_digest_mismatch")
        object.__setattr__(self, "run_digest", expected)

    def identity_payload(self) -> dict[str, Any]:
        return {
            "protocol_version": TOOL_PROTOCOL_VERSION,
            "run_id": self.run_id,
            "objective_id": self.objective_id,
            "objective_version": self.objective_version,
            "objective_digest": self.objective_digest,
            "plan_id": self.plan_id,
            "plan_version": self.plan_version,
            "plan_digest": self.plan_digest,
            "provider_id": self.provider_id,
            "provider_profile_version": self.provider_profile_version,
            "provider_profile_digest": self.provider_profile_digest,
            "authorization_id": self.authorization_id,
            "authorization_digest": self.authorization_digest,
            "status": self.status,
            "checkpoint_sequence": self.checkpoint_sequence,
            "last_safe_checkpoint": self.last_safe_checkpoint,
            "model_calls_used": self.model_calls_used,
            "tokens_used": self.tokens_used,
            "cost_used_microunits": self.cost_used_microunits,
            "steps_used": self.steps_used,
            "started_at": self.started_at,
            "last_activity_at": self.last_activity_at,
            "ended_at": self.ended_at,
            "failure_code": self.failure_code,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_payload(), "run_digest": self.run_digest}


def _create_cognitive_run(**values: Any) -> CognitiveRun:
    return CognitiveRun(**values, _factory_seal=_RUN_FACTORY_SEAL)


@dataclass(frozen=True)
class CognitiveRunCheckpoint:
    run_id: str
    checkpoint_sequence: int
    safe_to_resume: bool
    run_status: str
    authorization_digest: str | None
    objective_digest: str
    plan_digest: str
    provider_profile_digest: str
    current_node_ids: tuple[str, ...]
    completed_node_ids: tuple[str, ...]
    model_budget_state: Mapping[str, Any]
    runtime_recovery_bindings: tuple[Mapping[str, Any], ...]
    proposal_ids: tuple[str, ...]
    execution_ids: tuple[str, ...]
    idempotency_keys: tuple[str, ...]
    last_completed_execution_id: str | None
    unsafe_reason_codes: tuple[str, ...]
    steps_used: int
    started_at: str | None
    last_activity_at: str | None
    ended_at: str | None
    failure_code: str | None
    recorded_at: str
    checkpoint_digest: str = ""
    _factory_seal: InitVar[object | None] = None

    def __post_init__(self, _factory_seal: object | None) -> None:
        if _factory_seal is not _CHECKPOINT_FACTORY_SEAL:
            _fail("cognitive_checkpoint_factory_required")
        _identity(self.run_id, "invalid_cognitive_checkpoint_run_id")
        if (
            isinstance(self.checkpoint_sequence, bool)
            or not isinstance(self.checkpoint_sequence, int)
            or self.checkpoint_sequence < 1
        ):
            _fail("invalid_cognitive_checkpoint_sequence")
        if not isinstance(self.safe_to_resume, bool):
            _fail("invalid_cognitive_checkpoint_safety")
        if self.run_status not in RUN_STATUSES:
            _fail("invalid_cognitive_checkpoint_run_status")
        for value, code in (
            (self.objective_digest, "invalid_cognitive_checkpoint_objective_digest"),
            (self.plan_digest, "invalid_cognitive_checkpoint_plan_digest"),
            (
                self.provider_profile_digest,
                "invalid_cognitive_checkpoint_provider_digest",
            ),
        ):
            _sha256(value, code)
        if self.authorization_digest is not None:
            _sha256(
                self.authorization_digest,
                "invalid_cognitive_checkpoint_authorization_digest",
            )
        for field_name in (
            "current_node_ids",
            "completed_node_ids",
            "proposal_ids",
            "execution_ids",
            "idempotency_keys",
        ):
            values = tuple(sorted(set(getattr(self, field_name))))
            if len(values) != len(getattr(self, field_name)):
                _fail("duplicate_cognitive_checkpoint_identity")
            for value in values:
                _identity(value, "invalid_cognitive_checkpoint_identity")
            object.__setattr__(self, field_name, values)
        budget = _json_copy(self.model_budget_state, "invalid_checkpoint_budget_state")
        budget_fields = {
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
        if not isinstance(budget, dict) or set(budget) != budget_fields:
            _fail("invalid_checkpoint_budget_state")
        for field_name in (
            "calls_reserved",
            "input_tokens_reserved",
            "output_tokens_reserved",
            "cost_microunits_reserved",
            "actual_input_tokens",
            "actual_output_tokens",
            "actual_cost_microunits",
            "unknown_cost_reserved_ceiling_microunits",
        ):
            value = budget[field_name]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                _fail("invalid_checkpoint_budget_state")
        for field_name, validator in (
            ("request_digests", _sha256),
            ("settled_reservation_ids", _identity),
            ("unknown_cost_reservation_ids", _identity),
        ):
            values = budget[field_name]
            if (
                not isinstance(values, list)
                or len(set(values)) != len(values)
            ):
                _fail("invalid_checkpoint_budget_state")
            for value in values:
                validator(value, "invalid_checkpoint_budget_state")
        if (
            len(budget["request_digests"]) != budget["calls_reserved"]
            or not set(budget["unknown_cost_reservation_ids"]).issubset(
                budget["settled_reservation_ids"]
            )
            or budget["actual_input_tokens"]
            > budget["input_tokens_reserved"]
            or budget["actual_output_tokens"]
            > budget["output_tokens_reserved"]
            or budget["actual_cost_microunits"]
            + budget["unknown_cost_reserved_ceiling_microunits"]
            > budget["cost_microunits_reserved"]
        ):
            _fail("invalid_checkpoint_budget_state")
        object.__setattr__(self, "model_budget_state", _freeze(budget))
        recovery = _json_copy(
            list(self.runtime_recovery_bindings),
            "invalid_checkpoint_runtime_recovery",
        )
        if not isinstance(recovery, list) or len(recovery) > 64 or any(
            not isinstance(item, dict) for item in recovery
        ):
            _fail("invalid_checkpoint_runtime_recovery")
        object.__setattr__(
            self,
            "runtime_recovery_bindings",
            tuple(_freeze(item) for item in recovery),
        )
        if self.last_completed_execution_id is not None:
            _identity(
                self.last_completed_execution_id,
                "invalid_checkpoint_last_execution",
            )
        reasons = tuple(sorted(set(self.unsafe_reason_codes)))
        if self.safe_to_resume and reasons:
            _fail("safe_checkpoint_cannot_have_unsafe_reason")
        if not self.safe_to_resume and not reasons:
            _fail("unsafe_checkpoint_reason_required")
        object.__setattr__(self, "unsafe_reason_codes", reasons)
        if (
            isinstance(self.steps_used, bool)
            or not isinstance(self.steps_used, int)
            or self.steps_used < 0
        ):
            _fail("invalid_cognitive_checkpoint_steps")
        for field_name in ("started_at", "last_activity_at", "ended_at"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, normalize_timestamp(value))
        if self.failure_code is not None and (
            not isinstance(self.failure_code, str)
            or not self.failure_code
            or len(self.failure_code) > 128
        ):
            _fail("invalid_cognitive_checkpoint_failure_code")
        object.__setattr__(self, "recorded_at", normalize_timestamp(self.recorded_at))
        expected = canonical_digest(self.identity_payload())
        if self.checkpoint_digest and self.checkpoint_digest != expected:
            _fail("cognitive_checkpoint_digest_mismatch")
        object.__setattr__(self, "checkpoint_digest", expected)

    def identity_payload(self) -> dict[str, Any]:
        return {
            "protocol_version": TOOL_PROTOCOL_VERSION,
            "run_id": self.run_id,
            "checkpoint_sequence": self.checkpoint_sequence,
            "safe_to_resume": self.safe_to_resume,
            "run_status": self.run_status,
            "authorization_digest": self.authorization_digest,
            "objective_digest": self.objective_digest,
            "plan_digest": self.plan_digest,
            "provider_profile_digest": self.provider_profile_digest,
            "current_node_ids": list(self.current_node_ids),
            "completed_node_ids": list(self.completed_node_ids),
            "model_budget_state": _thaw(self.model_budget_state),
            "runtime_recovery_bindings": [
                _thaw(item) for item in self.runtime_recovery_bindings
            ],
            "proposal_ids": list(self.proposal_ids),
            "execution_ids": list(self.execution_ids),
            "idempotency_keys": list(self.idempotency_keys),
            "last_completed_execution_id": self.last_completed_execution_id,
            "unsafe_reason_codes": list(self.unsafe_reason_codes),
            "steps_used": self.steps_used,
            "started_at": self.started_at,
            "last_activity_at": self.last_activity_at,
            "ended_at": self.ended_at,
            "failure_code": self.failure_code,
            "recorded_at": self.recorded_at,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.identity_payload(),
            "checkpoint_digest": self.checkpoint_digest,
        }


def _create_cognitive_checkpoint(**values: Any) -> CognitiveRunCheckpoint:
    return CognitiveRunCheckpoint(
        **values,
        _factory_seal=_CHECKPOINT_FACTORY_SEAL,
    )


@dataclass(frozen=True)
class GatewayExecutionOutcome:
    result: ToolExecutionResult
    idempotent_replay: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.result, ToolExecutionResult):
            _fail("invalid_gateway_execution_result")
        if not isinstance(self.idempotent_replay, bool):
            _fail("invalid_gateway_replay_flag")


__all__ = [
    "CognitiveExecutionModelError",
    "CognitiveRun",
    "CognitiveRunCheckpoint",
    "GatewayExecutionOutcome",
    "INVOCATION_KINDS",
    "POLICY_DECISIONS",
    "RUN_STATUSES",
    "TOOL_PROTOCOL_VERSION",
    "ToolExecutionPreview",
    "ToolExecutionResult",
    "ToolProposal",
    "create_tool_proposal",
]
