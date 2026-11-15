"""Fail-closed v10-C bridge to the existing v8 production Runtime.

The production facade deliberately accepts no path, adapter, transport, or
service dependency.  It binds a typed, manually selected v10 provider and a
per-execution one-call cognitive reservation to the fixed v8 DeepSeek Runtime.
v10-D may bind several separately reserved calls to one bounded run-level
budget state, but each execute still has one send attempt and no automatic
retry or Provider fallback.  The v8 Runtime remains the only owner of
executions, artifacts, receipts, usage, and the ledger.
"""
from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import InitVar, dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from memory.cognitive_capability import CapabilityRegistry
from memory.cognitive_orchestration import (
    CognitiveModelRequest,
    CognitiveOrchestrationError,
    ModelBudgetState,
    ModelCallReservation,
    ParsedModelOutput,
    STRUCTURED_OUTPUT_CONTRACT_VERSION,
    parse_structured_model_output,
    settle_model_call,
)
from memory.cognitive_external_send import (
    ExternalSendConsent,
    ExternalSendConsentError,
    reservation_digest,
)
from memory.cognitive_provider import (
    CognitiveProviderError,
    ProviderSelection,
    suggested_provider_profiles,
)
from memory.operational_state_models import (
    OperationalStateError,
    make_identity,
    normalize_timestamp,
)
from memory.production_runtime_execution import (
    MockRuntimeExecutionService,
    ProductionRuntimeExecutionError,
    ProductionRuntimeExecutionService,
    ProductionRuntimeExecutionSession,
    RuntimePreviewMismatch,
    create_mock_runtime_execution_service,
    is_trusted_production_session,
)
from memory.runtime_protocol_contracts import (
    BOUNDED_TOOL_RUNTIME_REQUEST_VERSION,
    BoundedToolRuntimeRequest,
    FIXED_GENERATION_CONFIG,
    MODEL_ID as V8_MODEL_ID,
    PROVIDER_ID as V8_PROVIDER_ID,
    RuntimeProtocolContractError,
    canonical_digest,
    canonical_json,
    create_bounded_tool_runtime_request,
    receipt_identity,
    validate_reviewed_execution_binding,
)


BRIDGE_CONTRACT_VERSION = "v10-c-cognitive-runtime-bridge-1"
BRIDGE_RECONCILIATION_VERSION = "v10-d-cognitive-runtime-reconciliation-1"
EXTERNAL_SEND_DISCLOSURE_VERSION = "v10-c-external-send-disclosure-1"
PRODUCTION_PROVIDER_ID = V8_PROVIDER_ID
PRODUCTION_PROVIDER_PROFILE_VERSION = 1
PRODUCTION_MODEL_ID = V8_MODEL_ID
PRODUCTION_FIXED_OUTPUT_TOKENS = FIXED_GENERATION_CONFIG["max_tokens"]
PRODUCTION_BUDGET_PROFILE = "Small"
BOUNDED_TOOL_BUDGET_PROFILE = "Medium"
PRODUCTION_SECRET_REFERENCE = "secret.deepseek.production"
REQUIRED_CONTEXT_SOURCES = ("v7_memory", "v9_now", "v9_resume")
MAX_BOUNDED_MODEL_CALLS = 8
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_ID_RE = re.compile(r"[A-Za-z0-9._:-]{8,128}")
_QUESTION_LIMIT = 500
_BOUNDED_TOOL_BLUEPRINT_KEY = "bounded_tool_proposal_blueprint"
_MOCK_SCENARIOS = {
    "success",
    "preview_drift",
    "provider_failure",
    "usage_over_budget",
}
_DISCLOSURE_FACTORY_SEAL = object()
_PREVIEW_FACTORY_SEAL = object()
_OBSERVATION_FACTORY_SEAL = object()
_RECONCILIATION_FACTORY_SEAL = object()

_RECOVERY_REQUIRED_PHASES = frozenset(
    {
        "not_persisted",
        "not_attempted",
        "provider_response_persist_in_doubt",
        "provider_response_received_local_commit_pending",
        "runtime_read_failed",
        "send_attempt_in_doubt",
        "send_attempted_result_unknown",
    }
)
_TERMINAL_FAILED_PHASES = frozenset(
    {"not_attempted_recovered", "provider_failed"}
)


def _production_profile_digest() -> str:
    profiles = suggested_provider_profiles((PRODUCTION_SECRET_REFERENCE,))
    matches = [
        profile
        for profile in profiles
        if (
            profile.provider_id == PRODUCTION_PROVIDER_ID
            and profile.profile_version == PRODUCTION_PROVIDER_PROFILE_VERSION
            and profile.model == PRODUCTION_MODEL_ID
            and profile.adapter_kind == "deepseek_v8_runtime"
        )
    ]
    if len(matches) != 1:
        raise RuntimeError("production_provider_profile_contract_missing")
    return matches[0].profile_digest


PRODUCTION_PROVIDER_PROFILE_DIGEST = _production_profile_digest()


class CognitiveRuntimeBridgeError(ValueError):
    """Sanitized bridge contract or fail-closed execution error."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class CognitiveRuntimeRecoveryRequired(CognitiveRuntimeBridgeError):
    """Fail-closed signal for a nonterminal or ledger-uncertain v8 run."""

    state = "recovery_required"
    grants_authority = False
    execution_allowed = False
    system_verified = False
    user_accepted = False

    def __init__(
        self,
        *,
        runtime_model_run_id: str,
        execution_phase: str,
        cause_reason: str | None = None,
    ) -> None:
        super().__init__("cognitive_runtime_recovery_required")
        self.runtime_model_run_id = runtime_model_run_id
        self.execution_phase = execution_phase
        self.cause_reason = cause_reason

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "reason_code": self.reason_code,
            "runtime_model_run_id": self.runtime_model_run_id,
            "execution_phase": self.execution_phase,
            "cause_reason": self.cause_reason,
            "grants_authority": self.grants_authority,
            "execution_allowed": self.execution_allowed,
            "system_verified": self.system_verified,
            "user_accepted": self.user_accepted,
        }


def _fail(reason_code: str) -> None:
    raise CognitiveRuntimeBridgeError(reason_code)


def _recovery_required(
    runtime_model_run_id: str,
    execution_phase: str,
    *,
    cause_reason: str | None = None,
) -> None:
    raise CognitiveRuntimeRecoveryRequired(
        runtime_model_run_id=runtime_model_run_id,
        execution_phase=execution_phase,
        cause_reason=cause_reason,
    )


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _materialize(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _materialize(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_materialize(item) for item in value]
    if isinstance(value, list):
        return [_materialize(item) for item in value]
    return value


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return value


def _canonical_mapping(value: Any, reason_code: str) -> Mapping[str, Any]:
    materialized = _materialize(value)
    if not isinstance(materialized, dict):
        _fail(reason_code)
    try:
        normalized = json.loads(canonical_json(materialized))
    except (TypeError, ValueError, RecursionError):
        _fail(reason_code)
    return _freeze(normalized)


def _canonical_bounded_request_binding(
    value: Any,
    question: str,
    reason_code: str,
) -> Mapping[str, Any] | None:
    if value is None:
        return None
    normalized = _materialize(_canonical_mapping(value, reason_code))
    expected_fields = {
        "contract_version",
        "request_identity",
        "retrieval_question",
        "retrieval_question_sha256",
        "appendix_sha256",
        "appendix_utf8_bytes",
        "cognitive_request_digest",
        "plan_node_digest",
        "capability_digest",
        "context_bindings_digest",
    }
    if (
        set(normalized) != expected_fields
        or normalized["contract_version"] != BOUNDED_TOOL_RUNTIME_REQUEST_VERSION
        or normalized["retrieval_question"] != question
        or normalized["retrieval_question_sha256"] != _text_digest(question)
        or isinstance(normalized["appendix_utf8_bytes"], bool)
        or not isinstance(normalized["appendix_utf8_bytes"], int)
        or not 1 <= normalized["appendix_utf8_bytes"] <= 4_096
    ):
        _fail(reason_code)
    for name in (
        "retrieval_question_sha256",
        "appendix_sha256",
        "cognitive_request_digest",
        "plan_node_digest",
        "capability_digest",
        "context_bindings_digest",
    ):
        _digest(normalized[name], reason_code)
    identity_payload = {
        key: item
        for key, item in normalized.items()
        if key != "request_identity"
    }
    if normalized["request_identity"] != "btr_" + canonical_digest(identity_payload):
        _fail(reason_code)
    return _freeze(normalized)


def _digest(value: str, reason_code: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        _fail(reason_code)
    return value


def _identity(value: str, reason_code: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        _fail(reason_code)
    return value


def _text_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _timestamp_value(value: str, reason_code: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        _fail(reason_code)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _fail(reason_code)
    if parsed.tzinfo is None:
        _fail(reason_code)
    return parsed.astimezone(timezone.utc)


def _timestamp(value: Any, reason_code: str) -> str:
    try:
        return normalize_timestamp(value)
    except OperationalStateError:
        _fail(reason_code)


def _compact_digest(value: str) -> str:
    _digest(value, "invalid_bridge_source_digest")
    return base64.urlsafe_b64encode(bytes.fromhex(value)).decode("ascii").rstrip("=")


def _safe_summary(value: Any) -> str:
    """Return deterministic, delimiter-safe data, never an instruction field."""

    preferred = (
        "current_phase",
        "current_version",
        "next_approved_step",
        "project_purpose",
        "state",
        "status",
        "fact",
        "summary",
        "title",
        "text",
        "reason_code",
    )
    atoms: list[str] = []

    def collect(item: Any, *, key: str = "") -> None:
        if len(atoms) >= 8:
            return
        if isinstance(item, Mapping):
            keys = sorted(
                item,
                key=lambda candidate: (
                    preferred.index(str(candidate))
                    if str(candidate) in preferred
                    else len(preferred),
                    str(candidate),
                ),
            )
            for nested_key in keys:
                collect(item[nested_key], key=str(nested_key))
        elif isinstance(item, (list, tuple)):
            for nested in item[:4]:
                collect(nested, key=key)
        elif isinstance(item, (str, int, float, bool)) or item is None:
            text = str(item).strip()
            if text:
                atoms.append(f"{key}:{text}" if key else text)

    collect(value)
    raw = " ".join(atoms) or "selected-data"
    normalized = " ".join(raw.split())
    safe = "".join(
        char
        if char.isalnum() or char in " .,:_-/"
        else " "
        for char in normalized
    )
    return " ".join(safe.split()) or "selected-data"


def _shorten(value: str, maximum: int) -> str:
    if maximum < 4:
        _fail("bridge_question_budget_invalid")
    if len(value) <= maximum:
        return value
    return value[: maximum - 3].rstrip() + "..."


def _bounded_tool_blueprint(
    request: CognitiveModelRequest,
) -> dict[str, Any] | None:
    """Return one Plan-bound proposal template, or no extended tool request.

    The template is still untrusted model input and grants no authority.  The
    Agent Loop later parses the model response and independently revalidates
    it against the code-owned Capability Registry, persisted Plan, and
    Authorization Artifact before the Tool Gateway may execute anything.
    """

    context = _materialize(request.context.model_context)
    if not isinstance(context, dict):
        _fail("cognitive_context_control_mismatch")
    raw = context.get(_BOUNDED_TOOL_BLUEPRINT_KEY)
    if raw is None:
        return None
    if request.expected_output_kind != "tool_proposal":
        _fail("bounded_tool_blueprint_kind_mismatch")
    if not isinstance(raw, dict) or set(raw) != {
        "capability_id",
        "capability_version",
        "capability_digest",
        "typed_arguments",
        "reason",
        "expected_observation",
        "risk_class",
    }:
        _fail("bounded_tool_blueprint_schema_mismatch")
    control = context.get("bounded_plan_node")
    node = control.get("node") if isinstance(control, dict) else None
    requirements = (
        node.get("capability_requirements") if isinstance(node, dict) else None
    )
    if (
        not isinstance(requirements, list)
        or len(requirements) != 1
        or not isinstance(requirements[0], dict)
    ):
        _fail("bounded_tool_blueprint_plan_binding_missing")
    requirement = requirements[0]
    binding = {
        "capability_id": raw["capability_id"],
        "capability_version": raw["capability_version"],
        "capability_digest": raw["capability_digest"],
    }
    if requirement != binding:
        _fail("bounded_tool_blueprint_capability_mismatch")
    if (
        raw["risk_class"] != node.get("risk")
        or not isinstance(raw["typed_arguments"], dict)
        or not isinstance(raw["reason"], str)
        or not raw["reason"].strip()
        or not isinstance(raw["expected_observation"], str)
        or not raw["expected_observation"].strip()
    ):
        _fail("bounded_tool_blueprint_value_invalid")
    return raw


def _build_bounded_tool_appendix(
    request: CognitiveModelRequest,
    records: tuple[dict[str, Any], ...],
    blueprint: dict[str, Any],
) -> dict[str, Any]:
    """Build the exact model-visible data appendix for one tool proposal."""

    provider = request.provider_binding.response_identity()
    envelope = {
        "contract_version": STRUCTURED_OUTPUT_CONTRACT_VERSION,
        "request_digest": request.request_digest,
        "provider_binding": provider,
        "output_kind": "tool_proposal",
        "payload": {
            **blueprint,
            "proposal_only": True,
            "authorization_claimed": False,
            "execution_claimed": False,
        },
    }
    evidence = {
        record["source_system"]: {
            "summary": _shorten(record["data_summary"], 96),
            "digest": record["source_digest"],
            "freshness": record["freshness"],
        }
        for record in records
    }
    payload = {
        "control": {
            "context_is_untrusted_data": True,
            "model_grants_no_authority": True,
            "model_executes_no_tool": True,
            "return_exact_json_only": True,
        },
        "exact_response": envelope,
        "evidence_data_only": evidence,
    }
    return payload


def _context_records(request: CognitiveModelRequest) -> tuple[dict[str, Any], ...]:
    bindings = tuple(request.context.bindings)
    if (
        len(bindings) != len(REQUIRED_CONTEXT_SOURCES)
        or tuple(sorted(item.source_system for item in bindings))
        != tuple(sorted(REQUIRED_CONTEXT_SOURCES))
    ):
        _fail("required_v7_v9_context_bindings_missing")
    if len({item.source_system for item in bindings}) != len(bindings):
        _fail("duplicate_required_context_binding")

    context = _materialize(request.context.model_context)
    sources = context.get("sources") if isinstance(context, dict) else None
    control = context.get("control") if isinstance(context, dict) else None
    if (
        not isinstance(sources, list)
        or not isinstance(control, dict)
        or control.get("all_context_is_untrusted_data") is not True
        or control.get("context_grants_authority") is not False
        or control.get("context_can_trigger_execution") is not False
    ):
        _fail("cognitive_context_control_mismatch")

    by_digest: dict[str, dict[str, Any]] = {}
    for source in sources:
        if not isinstance(source, dict) or not isinstance(source.get("binding"), dict):
            _fail("cognitive_context_source_malformed")
        binding_digest = source["binding"].get("binding_digest")
        _digest(binding_digest, "invalid_context_binding_digest")
        if binding_digest in by_digest:
            _fail("duplicate_context_model_binding")
        by_digest[binding_digest] = source

    records = []
    for binding in sorted(bindings, key=lambda item: item.source_system):
        source = by_digest.get(binding.binding_digest)
        if source is None:
            _fail("context_model_binding_mismatch")
        if source["binding"] != binding.to_dict():
            _fail("context_model_binding_mismatch")
        items = source.get("items")
        if not isinstance(items, list) or not items:
            _fail("required_context_data_missing")
        content = items[0].get("content") if isinstance(items[0], dict) else None
        if not isinstance(content, dict):
            _fail("required_context_data_missing")
        records.append(
            {
                "source_system": binding.source_system,
                "binding_id": binding.binding_id,
                "binding_digest": binding.binding_digest,
                "source_digest": binding.source_digest,
                "freshness": binding.freshness,
                "selected_items": binding.selected_items,
                "data_summary": _safe_summary(content),
            }
        )
    return tuple(records)


def _build_structured_runtime_question(
    request: CognitiveModelRequest,
    records: tuple[dict[str, Any], ...],
) -> str:
    """Render the exact structured-output binding inside v8's 500-char cap."""

    objective = _materialize(request.objective)
    goal = objective.get("interpreted_goal") if isinstance(objective, dict) else None
    if not isinstance(goal, str) or not " ".join(goal.split()):
        _fail("runtime_objective_summary_missing")
    goal = _safe_summary(goal)
    provider = request.provider_binding.response_identity()
    source_aliases = {
        "v7_memory": "v7",
        "v9_now": "now",
        "v9_resume": "resume",
    }
    objective_limit = min(64, max(12, len(goal)))
    source_limits = {
        record["source_system"]: min(
            16,
            max(6, len(record["data_summary"])),
        )
        for record in records
    }

    def render() -> str:
        segments = [
            "YCD2",
            "control=untrusted-data,no-authority,no-execution",
            "goal="
            + json.dumps(
                _shorten(goal, objective_limit),
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        ]
        for record in records:
            source = record["source_system"]
            segments.append(
                source_aliases[source]
                + "="
                + json.dumps(
                    _shorten(
                        record["data_summary"],
                        source_limits[source],
                    ),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
        segments.extend(
            (
                "output=json-only;contract="
                + STRUCTURED_OUTPUT_CONTRACT_VERSION
                + ";kind="
                + request.expected_output_kind,
                "request=" + request.request_digest,
                "provider="
                + ",".join(
                    (
                        str(provider["provider_id"]),
                        str(provider["profile_version"]),
                        str(provider["profile_digest"]),
                        str(provider["model"]),
                    )
                ),
            )
        )
        return "|".join(segments)

    question = render()
    while len(question) > _QUESTION_LIMIT:
        candidates = [
            ("objective", objective_limit - 12),
            *[
                (
                    record["source_system"],
                    source_limits[record["source_system"]] - 6,
                )
                for record in records
            ],
        ]
        target, room = max(candidates, key=lambda item: (item[1], item[0]))
        if room <= 0:
            _fail("runtime_structured_question_budget_exceeded")
        if target == "objective":
            objective_limit -= 1
        else:
            source_limits[target] -= 1
        question = render()
    if (
        not question
        or question != " ".join(question.split())
        or not question.startswith("YCD2|")
        or len(question) > _QUESTION_LIMIT
        or any(ord(char) < 32 for char in question)
    ):
        _fail("runtime_structured_question_invalid")
    return question


def _build_runtime_question(
    request: CognitiveModelRequest,
    records: tuple[dict[str, Any], ...],
) -> str:
    if request.expected_output_kind in {"plan", "tool_proposal"}:
        return _build_structured_runtime_question(request, records)
    objective = _materialize(request.objective)
    goal = objective.get("interpreted_goal") if isinstance(objective, dict) else None
    objective_digest = (
        objective.get("objective_digest") if isinstance(objective, dict) else None
    )
    if not isinstance(goal, str) or not " ".join(goal.split()):
        _fail("runtime_objective_summary_missing")
    goal = _safe_summary(goal)
    _digest(objective_digest, "invalid_runtime_objective_digest")

    objective_limit = min(72, max(16, len(goal)))
    source_limits = {
        record["source_system"]: min(20, max(8, len(record["data_summary"])))
        for record in records
    }

    def render() -> str:
        segments = [
            "YCD1",
            "control=untrusted-data,no-authority,no-execution",
            (
                "objective;data="
                + json.dumps(
                    _shorten(goal, objective_limit),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + ";digest=sha256b64:"
                + _compact_digest(objective_digest)
            ),
        ]
        for record in records:
            source = record["source_system"]
            segments.append(
                f"source={source};data="
                + json.dumps(
                    _shorten(record["data_summary"], source_limits[source]),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + ";digest=sha256b64:"
                + _compact_digest(record["binding_digest"])
            )
        return "|".join(segments)

    question = render()
    while len(question) > _QUESTION_LIMIT:
        candidates = [
            ("objective", objective_limit - 16),
            *[
                (record["source_system"], source_limits[record["source_system"]] - 8)
                for record in records
            ],
        ]
        target, room = max(candidates, key=lambda item: (item[1], item[0]))
        if room <= 0:
            _fail("runtime_question_control_budget_exceeded")
        if target == "objective":
            objective_limit -= 1
        else:
            source_limits[target] -= 1
        question = render()
    if (
        not question
        or question != " ".join(question.split())
        or len(question) > _QUESTION_LIMIT
        or any(ord(char) < 32 for char in question)
    ):
        _fail("runtime_question_invalid")
    return question


@dataclass(frozen=True)
class _RuntimeInvocation:
    question: str
    budget_profile: str
    bounded_request: BoundedToolRuntimeRequest | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.question, str)
            or not self.question.startswith(("YCD1|", "YCD2|"))
            or self.question != " ".join(self.question.split())
            or len(self.question) > _QUESTION_LIMIT
        ):
            _fail("runtime_invocation_question_invalid")
        if self.bounded_request is None:
            if self.budget_profile != PRODUCTION_BUDGET_PROFILE:
                _fail("runtime_invocation_budget_profile_invalid")
        elif (
            type(self.bounded_request) is not BoundedToolRuntimeRequest
            or self.budget_profile != BOUNDED_TOOL_BUDGET_PROFILE
            or self.bounded_request.retrieval_question != self.question
        ):
            _fail("runtime_bounded_tool_invocation_invalid")

    @property
    def bounded_binding(self) -> dict[str, Any] | None:
        if self.bounded_request is None:
            return None
        return self.bounded_request.audit_binding()


def _build_runtime_invocation(
    request: CognitiveModelRequest,
    records: tuple[dict[str, Any], ...],
) -> _RuntimeInvocation:
    question = _build_runtime_question(request, records)
    blueprint = _bounded_tool_blueprint(request)
    if blueprint is None:
        return _RuntimeInvocation(
            question=question,
            budget_profile=PRODUCTION_BUDGET_PROFILE,
        )
    context = _materialize(request.context.model_context)
    control = context.get("bounded_plan_node") if isinstance(context, dict) else None
    node = control.get("node") if isinstance(control, dict) else None
    if not isinstance(node, dict):
        _fail("bounded_tool_blueprint_plan_binding_missing")
    node_digest = node.get("node_digest")
    if not isinstance(node_digest, str) or not _DIGEST_RE.fullmatch(node_digest):
        node_digest = canonical_digest(node)
    context_binding_digest = canonical_digest(
        [
            {
                "source_system": record["source_system"],
                "binding_id": record["binding_id"],
                "binding_digest": record["binding_digest"],
                "source_digest": record["source_digest"],
                "freshness": record["freshness"],
            }
            for record in records
        ]
    )
    try:
        bounded = create_bounded_tool_runtime_request(
            retrieval_question=question,
            appendix=_build_bounded_tool_appendix(request, records, blueprint),
            cognitive_request_digest=request.request_digest,
            plan_node_digest=node_digest,
            capability_digest=blueprint["capability_digest"],
            context_bindings_digest=context_binding_digest,
        )
    except RuntimeProtocolContractError as exc:
        _fail(exc.reason_code)
    return _RuntimeInvocation(
        question=question,
        budget_profile=BOUNDED_TOOL_BUDGET_PROFILE,
        bounded_request=bounded,
    )


def _budget_state_payload(state: ModelBudgetState) -> dict[str, Any]:
    if not isinstance(state, ModelBudgetState):
        _fail("typed_model_budget_state_required")
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
        "unknown_cost_reservation_ids": list(
            state.unknown_cost_reservation_ids
        ),
    }


def _validate_run_budget_state(
    request: CognitiveModelRequest,
    reservation: ModelCallReservation,
    state: ModelBudgetState,
    *,
    allow_settled: bool,
) -> str:
    payload = _budget_state_payload(state)
    budget = request.model_budget
    expected_reservation_id = make_identity(
        "modelres",
        {
            "request_digest": request.request_digest,
            "provider_binding_digest": (
                request.provider_binding.binding_digest
            ),
            "sequence": reservation.sequence,
            "estimated_input_tokens": reservation.input_tokens,
            "requested_output_tokens": reservation.output_token_ceiling,
            "estimated_cost_microunits": (
                reservation.cost_microunits_ceiling
            ),
        },
    )
    if (
        budget.maximum_model_calls < 1
        or budget.maximum_model_calls > MAX_BOUNDED_MODEL_CALLS
    ):
        _fail("bounded_model_call_budget_required")
    if (
        state.calls_reserved != len(state.request_digests)
        or state.calls_reserved > budget.maximum_model_calls
        or reservation.reservation_id != expected_reservation_id
        or reservation.sequence > state.calls_reserved
        or reservation.sequence > budget.maximum_model_calls
        or state.request_digests[reservation.sequence - 1]
        != request.request_digest
        or state.input_tokens_reserved > budget.maximum_input_tokens
        or state.output_tokens_reserved > budget.maximum_output_tokens
        or state.input_tokens_reserved + state.output_tokens_reserved
        > budget.maximum_total_tokens
        or state.cost_microunits_reserved
        > budget.maximum_cost_microunits
    ):
        _fail("model_budget_state_binding_mismatch")
    if (
        reservation.reservation_id in state.settled_reservation_ids
        and not allow_settled
    ):
        _fail("model_reservation_already_settled")
    return canonical_digest(payload)


def _validate_typed_contracts(
    selection: ProviderSelection,
    request: CognitiveModelRequest,
    reservation: ModelCallReservation,
    *,
    budget_state: ModelBudgetState | None = None,
    validate_health: bool = True,
    allow_settled: bool = False,
) -> tuple[dict[str, Any], ...]:
    if not isinstance(selection, ProviderSelection):
        _fail("typed_provider_selection_required")
    if not isinstance(request, CognitiveModelRequest):
        _fail("typed_cognitive_model_request_required")
    if not isinstance(reservation, ModelCallReservation):
        _fail("typed_model_call_reservation_required")
    if (
        selection.provider_id != PRODUCTION_PROVIDER_ID
        or selection.profile_version != PRODUCTION_PROVIDER_PROFILE_VERSION
        or selection.profile_digest != PRODUCTION_PROVIDER_PROFILE_DIGEST
        or selection.model != PRODUCTION_MODEL_ID
        or selection.selection_mode != "manual"
        or selection.fallback_used
        or selection.grants_authority
        or selection.health_attestation_digest is None
    ):
        _fail("production_provider_selection_mismatch")
    if validate_health:
        try:
            selection.validate_health_at(_utc_now())
        except CognitiveProviderError as exc:
            _fail(exc.reason_code)
    if request.provider_binding.selection != selection:
        _fail("cognitive_request_provider_selection_mismatch")
    if reservation.provider_binding != request.provider_binding:
        _fail("reservation_provider_binding_mismatch")
    if reservation.request_digest != request.request_digest:
        _fail("reservation_request_digest_mismatch")
    if budget_state is None:
        if reservation.sequence != 1:
            _fail("one_shot_reservation_required")
        if request.model_budget.maximum_model_calls != 1:
            _fail("one_model_call_budget_required")
        if (
            request.expected_output_kind != "answer"
            or _materialize(request.objective).get("agency_level")
            != "consult"
        ):
            _fail("v10_c_runtime_bridge_discussion_only")
    else:
        _validate_run_budget_state(
            request,
            reservation,
            budget_state,
            allow_settled=allow_settled,
        )
    if reservation.output_token_ceiling != PRODUCTION_FIXED_OUTPUT_TOKENS:
        _fail("fixed_runtime_output_budget_required")
    if (
        reservation.input_tokens > request.model_budget.maximum_input_tokens
        or reservation.output_token_ceiling
        > request.model_budget.maximum_output_tokens
        or reservation.input_tokens + reservation.output_token_ceiling
        > request.model_budget.maximum_total_tokens
        or reservation.cost_microunits_ceiling
        > request.model_budget.maximum_cost_microunits
    ):
        _fail("reservation_exceeds_model_budget")
    if request.mode != "ai":
        _fail("search_mode_provider_forbidden")
    return _context_records(request)


@dataclass(frozen=True)
class ExternalSendDisclosure:
    """Side-effect-free v8 preview of the exact outbound model delivery."""

    question: str
    budget_profile: str
    request_id: str
    mutation_id: str
    provider_selection_digest: str
    cognitive_request_digest: str
    reservation_id: str
    reservation_digest: str
    provider_binding_digest: str
    context_bindings: tuple[Mapping[str, Any], ...]
    runtime_reviewed_execution_binding: Mapping[str, Any]
    runtime_model_run_id: str
    runtime_artifact_identity: str
    prepared_at: str
    expires_at: str
    bounded_tool_request_binding: Mapping[str, Any] | None = None
    disclosure_digest: str = ""
    status: str = "disclosure"
    execution_phase: str = "not_attempted"
    contract_version: str = EXTERNAL_SEND_DISCLOSURE_VERSION
    _factory_seal: InitVar[object | None] = None

    def __post_init__(self, _factory_seal: object | None) -> None:
        if _factory_seal is not _DISCLOSURE_FACTORY_SEAL:
            _fail("external_send_disclosure_factory_required")
        if (
            self.contract_version != EXTERNAL_SEND_DISCLOSURE_VERSION
            or self.status != "disclosure"
            or self.execution_phase != "not_attempted"
        ):
            _fail("invalid_external_send_disclosure_state")
        if (
            not isinstance(self.question, str)
            or not self.question.startswith(("YCD1|", "YCD2|"))
            or len(self.question) > _QUESTION_LIMIT
            or self.question != " ".join(self.question.split())
        ):
            _fail("invalid_external_send_disclosure_question")
        bounded_binding = _canonical_bounded_request_binding(
            self.bounded_tool_request_binding,
            self.question,
            "invalid_external_send_disclosure_bounded_tool_binding",
        )
        expected_budget = (
            BOUNDED_TOOL_BUDGET_PROFILE
            if bounded_binding is not None
            else PRODUCTION_BUDGET_PROFILE
        )
        if self.budget_profile != expected_budget:
            _fail("invalid_external_send_disclosure_budget_profile")
        object.__setattr__(
            self,
            "bounded_tool_request_binding",
            bounded_binding,
        )
        _identity(self.request_id, "invalid_external_send_disclosure_request_id")
        _identity(self.mutation_id, "invalid_external_send_disclosure_mutation_id")
        _digest(
            self.provider_selection_digest,
            "invalid_external_send_disclosure_provider_digest",
        )
        _digest(
            self.cognitive_request_digest,
            "invalid_external_send_disclosure_request_digest",
        )
        _identity(self.reservation_id, "invalid_external_send_disclosure_reservation")
        _digest(
            self.reservation_digest,
            "invalid_external_send_disclosure_reservation_digest",
        )
        _digest(
            self.provider_binding_digest,
            "invalid_external_send_disclosure_provider_binding",
        )
        if len(self.context_bindings) != len(REQUIRED_CONTEXT_SOURCES):
            _fail("invalid_external_send_disclosure_context_references")
        context = tuple(
            _canonical_mapping(
                item,
                "invalid_external_send_disclosure_context_reference",
            )
            for item in self.context_bindings
        )
        object.__setattr__(self, "context_bindings", context)
        runtime_binding = _canonical_mapping(
            self.runtime_reviewed_execution_binding,
            "invalid_external_send_disclosure_runtime_binding",
        )
        try:
            validated = validate_reviewed_execution_binding(
                _materialize(runtime_binding)
            )
        except Exception:
            _fail("invalid_external_send_disclosure_runtime_binding")
        if (
            validated.get("request_id") != self.request_id
            or validated.get("mutation_id") != self.mutation_id
        ):
            _fail("external_send_disclosure_runtime_id_mismatch")
        reviewed_limits = (
            validated.get("reviewed_artifact", {})
            .get("budget", {})
            .get("limits", {})
        )
        reviewed_bounded_binding = (
            reviewed_limits.get("bounded_tool_binding")
            if isinstance(reviewed_limits, dict)
            else None
        )
        if bounded_binding is None:
            if reviewed_bounded_binding is not None:
                _fail("external_send_disclosure_bounded_tool_binding_mismatch")
        else:
            expected_bounded_binding = _materialize(bounded_binding)
            if (
                not isinstance(reviewed_bounded_binding, dict)
                or any(
                    reviewed_bounded_binding.get(key) != item
                    for key, item in expected_bounded_binding.items()
                )
            ):
                _fail("external_send_disclosure_bounded_tool_binding_mismatch")
        object.__setattr__(
            self,
            "runtime_reviewed_execution_binding",
            _freeze(validated),
        )
        _identity(
            self.runtime_model_run_id,
            "invalid_external_send_disclosure_model_run",
        )
        _identity(
            self.runtime_artifact_identity,
            "invalid_external_send_disclosure_artifact",
        )
        prepared_at = _timestamp(
            self.prepared_at,
            "invalid_external_send_disclosure_prepared_at",
        )
        expires_at = _timestamp(
            self.expires_at,
            "invalid_external_send_disclosure_expires_at",
        )
        prepared = _timestamp_value(
            prepared_at,
            "invalid_external_send_disclosure_prepared_at",
        )
        expires = _timestamp_value(
            expires_at,
            "invalid_external_send_disclosure_expires_at",
        )
        if expires <= prepared:
            _fail("external_send_disclosure_lifetime_invalid")
        object.__setattr__(self, "prepared_at", prepared_at)
        object.__setattr__(self, "expires_at", expires_at)
        expected = canonical_digest(self.identity_payload())
        if self.disclosure_digest:
            _digest(
                self.disclosure_digest,
                "invalid_external_send_disclosure_digest",
            )
            if self.disclosure_digest != expected:
                _fail("external_send_disclosure_digest_mismatch")
        object.__setattr__(self, "disclosure_digest", expected)

    @property
    def question_sha256(self) -> str:
        return _text_digest(self.question)

    @property
    def reviewed_binding_digest(self) -> str:
        return canonical_digest(
            _materialize(self.runtime_reviewed_execution_binding)
        )

    def identity_payload(self) -> dict[str, Any]:
        payload = {
            "contract_version": self.contract_version,
            "status": self.status,
            "execution_phase": self.execution_phase,
            "question_sha256": self.question_sha256,
            "budget_profile": self.budget_profile,
            "request_id": self.request_id,
            "mutation_id": self.mutation_id,
            "provider_selection_digest": self.provider_selection_digest,
            "cognitive_request_digest": self.cognitive_request_digest,
            "reservation_id": self.reservation_id,
            "reservation_digest": self.reservation_digest,
            "provider_binding_digest": self.provider_binding_digest,
            "context_bindings": [
                _materialize(item) for item in self.context_bindings
            ],
            "runtime_reviewed_execution_binding": _materialize(
                self.runtime_reviewed_execution_binding
            ),
            "runtime_model_run_id": self.runtime_model_run_id,
            "runtime_artifact_identity": self.runtime_artifact_identity,
            "prepared_at": self.prepared_at,
            "expires_at": self.expires_at,
        }
        if self.bounded_tool_request_binding is not None:
            payload["bounded_tool_request_binding"] = _materialize(
                self.bounded_tool_request_binding
            )
        return payload

    def consent_binding(self) -> dict[str, str]:
        return {
            "runtime_disclosure_digest": self.disclosure_digest,
            "runtime_disclosure_prepared_at": self.prepared_at,
            "runtime_disclosure_expires_at": self.expires_at,
            "runtime_request_id": self.request_id,
            "runtime_mutation_id": self.mutation_id,
            "runtime_budget_profile": self.budget_profile,
            "runtime_question_sha256": self.question_sha256,
            "runtime_reviewed_binding_digest": self.reviewed_binding_digest,
            "runtime_artifact_identity": self.runtime_artifact_identity,
            "runtime_model_run_id": self.runtime_model_run_id,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.identity_payload(),
            "question": self.question,
            "disclosure_digest": self.disclosure_digest,
            "preview_is_read_only": True,
            "credential_read": False,
            "network_request_count": 0,
            "persisted": False,
            "runtime_ledger_owner": "v8_runtime",
        }


@dataclass(frozen=True)
class CognitiveRuntimePreview:
    question: str
    budget_profile: str
    request_id: str
    mutation_id: str
    provider_selection_digest: str
    cognitive_request_digest: str
    reservation_id: str
    reservation_digest: str
    provider_binding_digest: str
    external_send_consent_digest: str
    runtime_disclosure_digest: str
    runtime_disclosure_prepared_at: str
    runtime_disclosure_expires_at: str
    context_bindings: tuple[Mapping[str, Any], ...]
    runtime_reviewed_execution_binding: Mapping[str, Any]
    runtime_model_run_id: str
    runtime_artifact_identity: str
    bounded_tool_request_binding: Mapping[str, Any] | None = None
    bridge_digest: str = ""
    status: str = "preview"
    execution_phase: str = "not_attempted"
    contract_version: str = BRIDGE_CONTRACT_VERSION
    _factory_seal: InitVar[object | None] = None

    def __post_init__(self, _factory_seal: object | None) -> None:
        if _factory_seal is not _PREVIEW_FACTORY_SEAL:
            _fail("cognitive_runtime_preview_factory_required")
        if (
            self.contract_version != BRIDGE_CONTRACT_VERSION
            or self.status != "preview"
            or self.execution_phase != "not_attempted"
        ):
            _fail("invalid_cognitive_runtime_preview_state")
        if (
            not isinstance(self.question, str)
            or not self.question.startswith(("YCD1|", "YCD2|"))
            or len(self.question) > _QUESTION_LIMIT
            or self.question != " ".join(self.question.split())
        ):
            _fail("invalid_cognitive_runtime_preview_question")
        bounded_binding = _canonical_bounded_request_binding(
            self.bounded_tool_request_binding,
            self.question,
            "invalid_cognitive_runtime_bounded_tool_binding",
        )
        expected_budget = (
            BOUNDED_TOOL_BUDGET_PROFILE
            if bounded_binding is not None
            else PRODUCTION_BUDGET_PROFILE
        )
        if self.budget_profile != expected_budget:
            _fail("invalid_cognitive_runtime_budget_profile")
        object.__setattr__(
            self,
            "bounded_tool_request_binding",
            bounded_binding,
        )
        _identity(self.request_id, "invalid_cognitive_runtime_request_id")
        _identity(self.mutation_id, "invalid_cognitive_runtime_mutation_id")
        _digest(
            self.provider_selection_digest,
            "invalid_provider_selection_digest",
        )
        _digest(self.cognitive_request_digest, "invalid_cognitive_request_digest")
        _identity(self.reservation_id, "invalid_model_reservation_id")
        _digest(self.reservation_digest, "invalid_model_reservation_digest")
        _digest(self.provider_binding_digest, "invalid_provider_binding_digest")
        _digest(
            self.external_send_consent_digest,
            "invalid_external_send_consent_digest",
        )
        _digest(
            self.runtime_disclosure_digest,
            "invalid_external_send_disclosure_digest",
        )
        if len(self.context_bindings) != len(REQUIRED_CONTEXT_SOURCES):
            _fail("invalid_cognitive_runtime_context_references")
        context = tuple(
            _canonical_mapping(item, "invalid_cognitive_runtime_context_reference")
            for item in self.context_bindings
        )
        object.__setattr__(self, "context_bindings", context)
        runtime_binding = _canonical_mapping(
            self.runtime_reviewed_execution_binding,
            "invalid_runtime_reviewed_execution_binding",
        )
        try:
            validated = validate_reviewed_execution_binding(
                _materialize(runtime_binding)
            )
        except Exception:
            _fail("invalid_runtime_reviewed_execution_binding")
        object.__setattr__(
            self,
            "runtime_reviewed_execution_binding",
            _freeze(validated),
        )
        if not isinstance(self.runtime_model_run_id, str) or not self.runtime_model_run_id:
            _fail("invalid_runtime_model_run_reference")
        if (
            not isinstance(self.runtime_artifact_identity, str)
            or not self.runtime_artifact_identity
        ):
            _fail("invalid_runtime_artifact_reference")
        disclosure = ExternalSendDisclosure(
            question=self.question,
            budget_profile=self.budget_profile,
            request_id=self.request_id,
            mutation_id=self.mutation_id,
            provider_selection_digest=self.provider_selection_digest,
            cognitive_request_digest=self.cognitive_request_digest,
            reservation_id=self.reservation_id,
            reservation_digest=self.reservation_digest,
            provider_binding_digest=self.provider_binding_digest,
            context_bindings=self.context_bindings,
            runtime_reviewed_execution_binding=(
                self.runtime_reviewed_execution_binding
            ),
            runtime_model_run_id=self.runtime_model_run_id,
            runtime_artifact_identity=self.runtime_artifact_identity,
            prepared_at=self.runtime_disclosure_prepared_at,
            expires_at=self.runtime_disclosure_expires_at,
            bounded_tool_request_binding=self.bounded_tool_request_binding,
            disclosure_digest=self.runtime_disclosure_digest,
            _factory_seal=_DISCLOSURE_FACTORY_SEAL,
        )
        if disclosure.disclosure_digest != self.runtime_disclosure_digest:
            _fail("cognitive_runtime_disclosure_drift")
        expected = canonical_digest(self.identity_payload())
        if self.bridge_digest and self.bridge_digest != expected:
            _fail("cognitive_runtime_preview_digest_mismatch")
        object.__setattr__(self, "bridge_digest", expected)

    def identity_payload(self) -> dict[str, Any]:
        payload = {
            "contract_version": self.contract_version,
            "status": self.status,
            "execution_phase": self.execution_phase,
            "question_sha256": _text_digest(self.question),
            "budget_profile": self.budget_profile,
            "request_id": self.request_id,
            "mutation_id": self.mutation_id,
            "provider_selection_digest": self.provider_selection_digest,
            "cognitive_request_digest": self.cognitive_request_digest,
            "reservation_id": self.reservation_id,
            "reservation_digest": self.reservation_digest,
            "provider_binding_digest": self.provider_binding_digest,
            "external_send_consent_digest": (
                self.external_send_consent_digest
            ),
            "runtime_disclosure_digest": self.runtime_disclosure_digest,
            "runtime_disclosure_prepared_at": (
                self.runtime_disclosure_prepared_at
            ),
            "runtime_disclosure_expires_at": (
                self.runtime_disclosure_expires_at
            ),
            "context_bindings": [
                _materialize(item) for item in self.context_bindings
            ],
            "runtime_reviewed_execution_binding": _materialize(
                self.runtime_reviewed_execution_binding
            ),
            "runtime_model_run_id": self.runtime_model_run_id,
            "runtime_artifact_identity": self.runtime_artifact_identity,
        }
        if self.bounded_tool_request_binding is not None:
            payload["bounded_tool_request_binding"] = _materialize(
                self.bounded_tool_request_binding
            )
        return payload

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.identity_payload(),
            "question": self.question,
            "bridge_digest": self.bridge_digest,
            "preview_is_read_only": True,
            "runtime_ledger_owner": "v8_runtime",
        }


@dataclass(frozen=True)
class CognitiveRuntimeExecutionReference:
    bridge_digest: str
    runtime_model_run_id: str
    runtime_receipt_identity: str | None
    runtime_artifact_identity: str
    runtime_usage_digest: str | None
    status: str
    execution_phase: str
    adapter_call_count_this_request: int
    model_call_count_for_run: int
    exactly_one_call: bool
    mock_transport: bool
    network_request_count: int
    credential_read: bool
    replayed: bool
    token_budget_verified: bool
    cost_accounting: str = "provider_cost_metadata_unavailable"
    automatic_retry: bool = False
    runtime_ledger_owner: str = "v8_runtime"
    contract_version: str = BRIDGE_CONTRACT_VERSION

    def __post_init__(self) -> None:
        _digest(self.bridge_digest, "invalid_cognitive_runtime_bridge_digest")
        if not isinstance(self.runtime_model_run_id, str) or not self.runtime_model_run_id:
            _fail("invalid_runtime_model_run_reference")
        if self.runtime_receipt_identity is not None and (
            not isinstance(self.runtime_receipt_identity, str)
            or not self.runtime_receipt_identity.startswith("rrc_")
        ):
            _fail("invalid_runtime_receipt_reference")
        if (
            not isinstance(self.runtime_artifact_identity, str)
            or not self.runtime_artifact_identity
        ):
            _fail("invalid_runtime_artifact_reference")
        if self.runtime_usage_digest is not None:
            _digest(self.runtime_usage_digest, "invalid_runtime_usage_reference")
        if self.status not in {"succeeded", "failed", "running", "prepared"}:
            _fail("invalid_runtime_execution_status")
        if (
            not isinstance(self.adapter_call_count_this_request, int)
            or isinstance(self.adapter_call_count_this_request, bool)
            or self.adapter_call_count_this_request not in {0, 1}
            or not isinstance(self.model_call_count_for_run, int)
            or isinstance(self.model_call_count_for_run, bool)
            or self.model_call_count_for_run not in {0, 1}
        ):
            _fail("runtime_one_shot_transport_violation")
        if (
            not isinstance(self.exactly_one_call, bool)
            or self.exactly_one_call
            is not (self.model_call_count_for_run == 1)
            or not isinstance(self.mock_transport, bool)
            or not isinstance(self.network_request_count, int)
            or isinstance(self.network_request_count, bool)
            or self.network_request_count not in {0, 1}
            or not isinstance(self.credential_read, bool)
        ):
            _fail("runtime_transport_evidence_invalid")
        if self.mock_transport:
            if self.network_request_count != 0 or self.credential_read:
                _fail("mock_runtime_transport_boundary_violation")
        elif (
            self.network_request_count
            != self.adapter_call_count_this_request
            or self.credential_read
            is not (self.adapter_call_count_this_request == 1)
        ):
            _fail("production_runtime_transport_boundary_violation")
        if self.replayed is not (
            self.adapter_call_count_this_request == 0
            and self.model_call_count_for_run == 1
        ):
            _fail("runtime_replay_reference_mismatch")
        if self.automatic_retry is not False:
            _fail("runtime_retry_forbidden")
        if self.runtime_ledger_owner != "v8_runtime":
            _fail("runtime_ledger_owner_mismatch")

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "bridge_digest": self.bridge_digest,
            "status": self.status,
            "execution_phase": self.execution_phase,
            "runtime_references": {
                "ledger_owner": self.runtime_ledger_owner,
                "model_run_id": self.runtime_model_run_id,
                "receipt_identity": self.runtime_receipt_identity,
                "artifact_identity": self.runtime_artifact_identity,
                "usage_digest": self.runtime_usage_digest,
            },
            "transport": {
                "adapter_call_count_this_request": (
                    self.adapter_call_count_this_request
                ),
                "model_call_count_for_run": self.model_call_count_for_run,
                "exactly_one_call": self.exactly_one_call,
                "automatic_retry": self.automatic_retry,
                "mock_transport": self.mock_transport,
                "network_request_count": self.network_request_count,
                "credential_read": self.credential_read,
                "replayed": self.replayed,
            },
            "budget_verification": {
                "token_budget_verified": self.token_budget_verified,
                "cost_accounting": self.cost_accounting,
            },
            "duplicates_runtime_receipt": False,
            "duplicates_runtime_artifact": False,
            "duplicates_runtime_usage": False,
            "contains_model_response": False,
        }


@dataclass(frozen=True)
class CognitiveRuntimeModelObservation:
    """Transient, untrusted model output read from the authoritative v8 run."""

    bridge_digest: str
    request_digest: str
    reservation_id: str
    reservation_digest: str
    reservation_sequence: int
    budget_state_digest: str
    runtime_model_run_id: str
    runtime_receipt_identity: str
    runtime_artifact_identity: str
    status: str
    execution_phase: str
    response_content: str | None
    usage: Mapping[str, Any] | None
    error: Mapping[str, Any] | None
    sent_to_model: bool
    external_model_calls: bool
    trust_class: str = "untrusted_model_output"
    grants_authority: bool = False
    execution_allowed: bool = False
    system_verified: bool = False
    user_accepted: bool = False
    runtime_ledger_owner: str = "v8_runtime"
    contract_version: str = BRIDGE_RECONCILIATION_VERSION
    observation_digest: str = ""
    _factory_seal: InitVar[object | None] = None

    def __post_init__(self, _factory_seal: object | None) -> None:
        if _factory_seal is not _OBSERVATION_FACTORY_SEAL:
            _fail("cognitive_runtime_observation_factory_required")
        for value, reason in (
            (self.bridge_digest, "invalid_cognitive_runtime_bridge_digest"),
            (self.request_digest, "invalid_cognitive_request_digest"),
            (self.reservation_digest, "invalid_model_reservation_digest"),
            (self.budget_state_digest, "invalid_model_budget_state_digest"),
        ):
            _digest(value, reason)
        _identity(self.reservation_id, "invalid_model_reservation_id")
        _identity(self.runtime_model_run_id, "invalid_runtime_model_run_reference")
        _identity(self.runtime_artifact_identity, "invalid_runtime_artifact_reference")
        if (
            not isinstance(self.runtime_receipt_identity, str)
            or not self.runtime_receipt_identity.startswith("rrc_")
        ):
            _fail("invalid_runtime_receipt_reference")
        if (
            isinstance(self.reservation_sequence, bool)
            or not isinstance(self.reservation_sequence, int)
            or self.reservation_sequence < 1
            or self.reservation_sequence > MAX_BOUNDED_MODEL_CALLS
        ):
            _fail("invalid_model_reservation_sequence")
        if (
            self.trust_class != "untrusted_model_output"
            or self.grants_authority is not False
            or self.execution_allowed is not False
            or self.system_verified is not False
            or self.user_accepted is not False
            or self.runtime_ledger_owner != "v8_runtime"
            or self.contract_version != BRIDGE_RECONCILIATION_VERSION
        ):
            _fail("runtime_model_observation_trust_violation")
        if not isinstance(self.sent_to_model, bool) or not isinstance(
            self.external_model_calls,
            bool,
        ):
            _fail("runtime_model_observation_execution_invalid")
        if self.sent_to_model is not self.external_model_calls:
            _fail("runtime_model_observation_execution_mismatch")

        usage: Mapping[str, Any] | None = None
        error: Mapping[str, Any] | None = None
        if self.status == "succeeded":
            if (
                self.execution_phase != "succeeded"
                or not isinstance(self.response_content, str)
                or not self.response_content
                or not self.sent_to_model
                or self.error is not None
                or not isinstance(self.usage, Mapping)
            ):
                _fail("runtime_success_observation_invalid")
            usage = _canonical_mapping(
                self.usage,
                "runtime_success_usage_invalid",
            )
            materialized_usage = _materialize(usage)
            if (
                set(materialized_usage)
                != {"prompt_tokens", "completion_tokens", "total_tokens"}
                or any(
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or value < 0
                    for value in materialized_usage.values()
                )
                or materialized_usage["total_tokens"]
                != materialized_usage["prompt_tokens"]
                + materialized_usage["completion_tokens"]
            ):
                _fail("runtime_success_usage_invalid")
        elif self.status == "failed":
            if (
                self.execution_phase not in _TERMINAL_FAILED_PHASES
                or self.response_content is not None
                or self.usage is not None
                or not isinstance(self.error, Mapping)
                or self.sent_to_model
                is not (self.execution_phase == "provider_failed")
            ):
                _fail("runtime_failed_observation_invalid")
            error = _canonical_mapping(
                self.error,
                "runtime_failed_error_invalid",
            )
            materialized_error = _materialize(error)
            if (
                set(materialized_error)
                != {
                    "error_id",
                    "category",
                    "code",
                    "retryable",
                    "http_status",
                }
                or not isinstance(materialized_error["code"], str)
                or not materialized_error["code"]
                or not isinstance(materialized_error["retryable"], bool)
            ):
                _fail("runtime_failed_error_invalid")
        else:
            _fail("runtime_model_observation_nonterminal")
        object.__setattr__(self, "usage", usage)
        object.__setattr__(self, "error", error)
        expected = canonical_digest(self.identity_payload())
        if self.observation_digest:
            _digest(
                self.observation_digest,
                "invalid_runtime_model_observation_digest",
            )
            if self.observation_digest != expected:
                _fail("runtime_model_observation_digest_mismatch")
        object.__setattr__(self, "observation_digest", expected)

    @property
    def response_content_sha256(self) -> str | None:
        if self.response_content is None:
            return None
        return _text_digest(self.response_content)

    def identity_payload(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "bridge_digest": self.bridge_digest,
            "request_digest": self.request_digest,
            "reservation_id": self.reservation_id,
            "reservation_digest": self.reservation_digest,
            "reservation_sequence": self.reservation_sequence,
            "budget_state_digest": self.budget_state_digest,
            "runtime_model_run_id": self.runtime_model_run_id,
            "runtime_receipt_identity": self.runtime_receipt_identity,
            "runtime_artifact_identity": self.runtime_artifact_identity,
            "status": self.status,
            "execution_phase": self.execution_phase,
            "response_content_sha256": self.response_content_sha256,
            "usage": None if self.usage is None else _materialize(self.usage),
            "error": None if self.error is None else _materialize(self.error),
            "sent_to_model": self.sent_to_model,
            "external_model_calls": self.external_model_calls,
            "trust_class": self.trust_class,
            "grants_authority": self.grants_authority,
            "execution_allowed": self.execution_allowed,
            "system_verified": self.system_verified,
            "user_accepted": self.user_accepted,
            "runtime_ledger_owner": self.runtime_ledger_owner,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.identity_payload(),
            "observation_digest": self.observation_digest,
            "response_content_serialized": False,
        }


@dataclass(frozen=True)
class CognitiveRuntimeReconciliation:
    """Budget settlement plus a proposal-only parsed model observation."""

    observation: CognitiveRuntimeModelObservation
    budget_state: ModelBudgetState
    state: str
    reason_code: str | None
    parsed_output: ParsedModelOutput | None
    automatic_retry: bool = False
    automatic_provider_fallback: bool = False
    grants_authority: bool = False
    execution_allowed: bool = False
    system_verified: bool = False
    user_accepted: bool = False
    runtime_ledger_owner: str = "v8_runtime"
    contract_version: str = BRIDGE_RECONCILIATION_VERSION
    reconciliation_digest: str = ""
    _factory_seal: InitVar[object | None] = None

    def __post_init__(self, _factory_seal: object | None) -> None:
        if _factory_seal is not _RECONCILIATION_FACTORY_SEAL:
            _fail("cognitive_runtime_reconciliation_factory_required")
        if not isinstance(self.observation, CognitiveRuntimeModelObservation):
            _fail("typed_runtime_model_observation_required")
        budget_payload = _budget_state_payload(self.budget_state)
        if (
            self.observation.reservation_id
            not in self.budget_state.settled_reservation_ids
        ):
            _fail("runtime_reconciliation_budget_not_settled")
        if self.state == "reconciled_succeeded":
            if (
                self.observation.status != "succeeded"
                or not isinstance(self.parsed_output, ParsedModelOutput)
                or self.reason_code is not None
                or self.parsed_output.request_digest
                != self.observation.request_digest
            ):
                _fail("runtime_success_reconciliation_invalid")
        elif self.state == "rejected_model_output":
            if (
                self.observation.status != "succeeded"
                or self.parsed_output is not None
                or not isinstance(self.reason_code, str)
                or not self.reason_code
            ):
                _fail("runtime_rejected_output_reconciliation_invalid")
        elif self.state == "reconciled_failed":
            if (
                self.observation.status != "failed"
                or self.parsed_output is not None
                or not isinstance(self.reason_code, str)
                or not self.reason_code
            ):
                _fail("runtime_failed_reconciliation_invalid")
        else:
            _fail("invalid_runtime_reconciliation_state")
        if (
            self.automatic_retry is not False
            or self.automatic_provider_fallback is not False
            or self.grants_authority is not False
            or self.execution_allowed is not False
            or self.system_verified is not False
            or self.user_accepted is not False
            or self.runtime_ledger_owner != "v8_runtime"
            or self.contract_version != BRIDGE_RECONCILIATION_VERSION
        ):
            _fail("runtime_reconciliation_trust_violation")
        expected = canonical_digest(
            self.identity_payload(budget_payload=budget_payload)
        )
        if self.reconciliation_digest:
            _digest(
                self.reconciliation_digest,
                "invalid_runtime_reconciliation_digest",
            )
            if self.reconciliation_digest != expected:
                _fail("runtime_reconciliation_digest_mismatch")
        object.__setattr__(self, "reconciliation_digest", expected)

    def identity_payload(
        self,
        *,
        budget_payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if budget_payload is None:
            budget_payload = _budget_state_payload(self.budget_state)
        return {
            "contract_version": self.contract_version,
            "state": self.state,
            "reason_code": self.reason_code,
            "observation_digest": self.observation.observation_digest,
            "budget_state": dict(budget_payload),
            "parsed_output": (
                None
                if self.parsed_output is None
                else self.parsed_output.to_dict()
            ),
            "automatic_retry": self.automatic_retry,
            "automatic_provider_fallback": self.automatic_provider_fallback,
            "grants_authority": self.grants_authority,
            "execution_allowed": self.execution_allowed,
            "system_verified": self.system_verified,
            "user_accepted": self.user_accepted,
            "runtime_ledger_owner": self.runtime_ledger_owner,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.identity_payload(),
            "reconciliation_digest": self.reconciliation_digest,
        }


def _validate_observation_preview(
    preview: CognitiveRuntimePreview,
    request: CognitiveModelRequest,
    reservation: ModelCallReservation,
    budget_state: ModelBudgetState,
) -> str:
    if not isinstance(preview, CognitiveRuntimePreview):
        _fail("typed_cognitive_runtime_preview_required")
    selection = request.provider_binding.selection
    records = _validate_typed_contracts(
        selection,
        request,
        reservation,
        budget_state=budget_state,
        validate_health=False,
        allow_settled=True,
    )
    invocation = _build_runtime_invocation(request, records)
    if (
        preview.provider_selection_digest != selection.selection_digest
        or preview.cognitive_request_digest != request.request_digest
        or preview.reservation_id != reservation.reservation_id
        or preview.reservation_digest != reservation_digest(reservation)
        or preview.provider_binding_digest
        != request.provider_binding.binding_digest
        or preview.question != invocation.question
        or preview.budget_profile != invocation.budget_profile
        or preview.bounded_tool_request_binding
        != (
            None
            if invocation.bounded_binding is None
            else _freeze(invocation.bounded_binding)
        )
        or preview.bridge_digest != canonical_digest(preview.identity_payload())
    ):
        _fail("cognitive_runtime_observation_binding_mismatch")
    return _validate_run_budget_state(
        request,
        reservation,
        budget_state,
        allow_settled=True,
    )


def _read_model_observation(
    runtime: Any,
    preview: CognitiveRuntimePreview,
    request: CognitiveModelRequest,
    reservation: ModelCallReservation,
    *,
    budget_state: ModelBudgetState,
) -> CognitiveRuntimeModelObservation:
    budget_state_digest = _validate_observation_preview(
        preview,
        request,
        reservation,
        budget_state,
    )
    try:
        result = runtime.get_run(preview.runtime_model_run_id)
    except ProductionRuntimeExecutionError as exc:
        phase = (
            "not_persisted"
            if exc.reason_code == "model_run_not_found"
            else "runtime_read_failed"
        )
        _recovery_required(
            preview.runtime_model_run_id,
            phase,
            cause_reason=exc.reason_code,
        )
    if not isinstance(result, dict):
        _recovery_required(
            preview.runtime_model_run_id,
            "runtime_read_failed",
            cause_reason="runtime_result_invalid",
        )
    artifact = result.get("request_artifact")
    artifact_identity = (
        artifact.get("artifact_identity") if isinstance(artifact, dict) else None
    )
    if (
        result.get("model_run_id") != preview.runtime_model_run_id
        or result.get("request_id") != preview.request_id
        or result.get("mutation_id") != preview.mutation_id
        or result.get("provider") != PRODUCTION_PROVIDER_ID
        or result.get("model") != PRODUCTION_MODEL_ID
        or artifact_identity != preview.runtime_artifact_identity
    ):
        _recovery_required(
            preview.runtime_model_run_id,
            "runtime_read_failed",
            cause_reason="runtime_observation_binding_mismatch",
        )
    status = result.get("status")
    phase = result.get("execution_phase")
    if status in {"prepared", "running"}:
        _recovery_required(
            preview.runtime_model_run_id,
            phase if isinstance(phase, str) else "runtime_read_failed",
            cause_reason=f"nonterminal_status:{status}",
        )
    if phase in _RECOVERY_REQUIRED_PHASES:
        _recovery_required(
            preview.runtime_model_run_id,
            phase,
            cause_reason="runtime_ledger_not_terminal_certain",
        )
    if status not in {"succeeded", "failed"} or not isinstance(phase, str):
        _recovery_required(
            preview.runtime_model_run_id,
            "runtime_read_failed",
            cause_reason="runtime_status_or_phase_unknown",
        )
    if status == "succeeded" and phase != "succeeded":
        _recovery_required(
            preview.runtime_model_run_id,
            "runtime_read_failed",
            cause_reason="runtime_success_phase_mismatch",
        )
    if status == "failed" and phase not in _TERMINAL_FAILED_PHASES:
        _recovery_required(
            preview.runtime_model_run_id,
            phase,
            cause_reason="runtime_failure_phase_uncertain",
        )
    receipt = result.get("receipt_identity")
    try:
        expected_receipt = receipt_identity(result)
    except Exception:
        _recovery_required(
            preview.runtime_model_run_id,
            "runtime_read_failed",
            cause_reason="runtime_receipt_invalid",
        )
    if not isinstance(receipt, str) or receipt != expected_receipt:
        _recovery_required(
            preview.runtime_model_run_id,
            "runtime_read_failed",
            cause_reason="runtime_receipt_identity_mismatch",
        )
    execution = result.get("execution")
    if (
        not isinstance(execution, dict)
        or execution.get("persisted") is not True
        or not isinstance(execution.get("sent_to_model"), bool)
        or not isinstance(execution.get("external_model_calls"), bool)
        or execution.get("sent_to_model")
        is not execution.get("external_model_calls")
    ):
        _recovery_required(
            preview.runtime_model_run_id,
            "runtime_read_failed",
            cause_reason="runtime_execution_projection_invalid",
        )
    response_content: str | None = None
    usage = result.get("usage")
    error = result.get("error")
    if status == "succeeded":
        response = result.get("response")
        response_content = (
            response.get("content") if isinstance(response, dict) else None
        )
        if (
            not isinstance(response_content, str)
            or not response_content
            or len(response_content.encode("utf-8"))
            > request.maximum_output_bytes
        ):
            _recovery_required(
                preview.runtime_model_run_id,
                "runtime_read_failed",
                cause_reason=(
                    "runtime_model_observation_output_budget_exceeded"
                ),
            )
    elif result.get("response") is not None:
        _recovery_required(
            preview.runtime_model_run_id,
            "runtime_read_failed",
            cause_reason="runtime_failed_response_present",
        )
    if status == "failed":
        error_code = error.get("code") if isinstance(error, Mapping) else None
        phase_code_valid = (
            phase == "not_attempted_recovered"
            and error_code == "interrupted_before_send"
        ) or (
            phase == "provider_failed"
            and error_code
            not in {
                "interrupted_before_send",
                "interrupted_run_in_doubt",
                "provider_response_persist_in_doubt",
            }
        )
        if not phase_code_valid:
            _recovery_required(
                preview.runtime_model_run_id,
                "runtime_read_failed",
                cause_reason="runtime_failure_phase_code_mismatch",
            )
    try:
        return CognitiveRuntimeModelObservation(
            bridge_digest=preview.bridge_digest,
            request_digest=request.request_digest,
            reservation_id=reservation.reservation_id,
            reservation_digest=reservation_digest(reservation),
            reservation_sequence=reservation.sequence,
            budget_state_digest=budget_state_digest,
            runtime_model_run_id=preview.runtime_model_run_id,
            runtime_receipt_identity=receipt,
            runtime_artifact_identity=artifact_identity,
            status=status,
            execution_phase=phase,
            response_content=response_content,
            usage=usage,
            error=error,
            sent_to_model=execution["sent_to_model"],
            external_model_calls=execution["external_model_calls"],
            _factory_seal=_OBSERVATION_FACTORY_SEAL,
        )
    except CognitiveRuntimeBridgeError as exc:
        _recovery_required(
            preview.runtime_model_run_id,
            "runtime_read_failed",
            cause_reason=exc.reason_code,
        )


def _reconcile_model_run(
    runtime: Any,
    preview: CognitiveRuntimePreview,
    request: CognitiveModelRequest,
    reservation: ModelCallReservation,
    *,
    budget_state: ModelBudgetState,
    capability_registry: CapabilityRegistry | None = None,
    seen_output_digests: Iterable[str] = (),
    seen_request_digests: Iterable[str] = (),
) -> CognitiveRuntimeReconciliation:
    observation = _read_model_observation(
        runtime,
        preview,
        request,
        reservation,
        budget_state=budget_state,
    )
    if reservation.reservation_id in budget_state.settled_reservation_ids:
        _fail("model_run_reconciliation_replay")
    if observation.status == "succeeded":
        usage = _materialize(observation.usage)
        actual_input_tokens = usage["prompt_tokens"]
        actual_output_tokens = usage["completion_tokens"]
        actual_cost_microunits: int | None = None
    elif observation.sent_to_model:
        actual_input_tokens = reservation.input_tokens
        actual_output_tokens = reservation.output_token_ceiling
        actual_cost_microunits = None
    else:
        actual_input_tokens = 0
        actual_output_tokens = 0
        actual_cost_microunits = 0
    try:
        settled = settle_model_call(
            reservation=reservation,
            state=budget_state,
            actual_provider=request.provider_binding.response_identity(),
            actual_input_tokens=actual_input_tokens,
            actual_output_tokens=actual_output_tokens,
            actual_cost_microunits=actual_cost_microunits,
            budget=request.model_budget,
        )
    except CognitiveOrchestrationError as exc:
        _recovery_required(
            preview.runtime_model_run_id,
            "runtime_read_failed",
            cause_reason=f"runtime_budget_settlement_failed:{exc.reason_code}",
        )

    if observation.status == "failed":
        error = _materialize(observation.error)
        return CognitiveRuntimeReconciliation(
            observation=observation,
            budget_state=settled,
            state="reconciled_failed",
            reason_code=error["code"],
            parsed_output=None,
            _factory_seal=_RECONCILIATION_FACTORY_SEAL,
        )
    try:
        parsed = parse_structured_model_output(
            observation.response_content,
            request=request,
            capability_registry=capability_registry,
            seen_output_digests=seen_output_digests,
            seen_request_digests=seen_request_digests,
        )
    except CognitiveOrchestrationError as exc:
        return CognitiveRuntimeReconciliation(
            observation=observation,
            budget_state=settled,
            state="rejected_model_output",
            reason_code=exc.reason_code,
            parsed_output=None,
            _factory_seal=_RECONCILIATION_FACTORY_SEAL,
        )
    return CognitiveRuntimeReconciliation(
        observation=observation,
        budget_state=settled,
        state="reconciled_succeeded",
        reason_code=None,
        parsed_output=parsed,
        _factory_seal=_RECONCILIATION_FACTORY_SEAL,
    )


def _prepare_disclosure(
    runtime: Any,
    selection: ProviderSelection,
    request: CognitiveModelRequest,
    reservation: ModelCallReservation,
    *,
    request_id: str,
    mutation_id: str,
    budget_state: ModelBudgetState | None = None,
) -> ExternalSendDisclosure:
    prepared_at = _utc_now()
    records = _validate_typed_contracts(
        selection,
        request,
        reservation,
        budget_state=budget_state,
    )
    invocation = _build_runtime_invocation(request, records)
    try:
        if invocation.bounded_request is None:
            runtime_preview = runtime.preview(
                invocation.question,
                budget_profile=invocation.budget_profile,
                request_id=request_id,
                mutation_id=mutation_id,
            )
        else:
            runtime_preview = runtime.preview_bounded_tool(
                invocation.bounded_request,
                request_id=request_id,
                mutation_id=mutation_id,
            )
    except ProductionRuntimeExecutionError as exc:
        _fail(exc.reason_code)
    if (
        runtime_preview.get("status") != "preview"
        or runtime_preview.get("execution_phase") != "not_attempted"
        or runtime_preview.get("question") != invocation.question
        or runtime_preview.get("request_id") != request_id
        or runtime_preview.get("mutation_id") != mutation_id
        or runtime_preview.get("usage") is not None
        or runtime_preview.get("response") is not None
        or runtime_preview.get("error") is not None
    ):
        _fail("runtime_preview_contract_mismatch")
    returned_bounded_binding = runtime_preview.get(
        "bounded_tool_request_binding"
    )
    if returned_bounded_binding != invocation.bounded_binding:
        _fail("runtime_preview_bounded_tool_binding_mismatch")
    execution = runtime_preview.get("execution")
    if (
        not isinstance(execution, dict)
        or execution.get("persisted") is not False
        or execution.get("credential_read") is not False
        or execution.get("sent_to_model") is not False
        or execution.get("external_model_calls") is not False
        or execution.get("network_request_count") != 0
        or execution.get("automatic_retry") is not False
    ):
        _fail("runtime_preview_side_effect_detected")
    profile = runtime_preview.get("runtime_profile")
    if (
        not isinstance(profile, dict)
        or profile.get("provider") != PRODUCTION_PROVIDER_ID
        or profile.get("model") != PRODUCTION_MODEL_ID
    ):
        _fail("runtime_profile_selection_mismatch")
    review = runtime_preview.get("review")
    if (
        not isinstance(review, dict)
        or not isinstance(review.get("estimated_tokens"), int)
        or review["estimated_tokens"] > reservation.input_tokens
    ):
        _fail("runtime_preview_exceeds_input_reservation")
    diagnostic = runtime_preview.get("diagnostic_bindings")
    if not isinstance(diagnostic, dict):
        _fail("runtime_preview_diagnostic_missing")
    runtime_binding = runtime_preview.get("reviewed_execution_binding")
    try:
        runtime_binding = validate_reviewed_execution_binding(runtime_binding)
    except Exception:
        _fail("invalid_runtime_reviewed_execution_binding")
    context_refs = tuple(
        {
            "source_system": record["source_system"],
            "binding_id": record["binding_id"],
            "binding_digest": record["binding_digest"],
            "source_digest": record["source_digest"],
            "freshness": record["freshness"],
            "selected_items": record["selected_items"],
        }
        for record in records
    )
    health = selection.health_attestation
    if health is None:
        _fail("provider_health_attestation_required")
    return ExternalSendDisclosure(
        question=invocation.question,
        budget_profile=invocation.budget_profile,
        request_id=request_id,
        mutation_id=mutation_id,
        provider_selection_digest=selection.selection_digest,
        cognitive_request_digest=request.request_digest,
        reservation_id=reservation.reservation_id,
        reservation_digest=reservation_digest(reservation),
        provider_binding_digest=request.provider_binding.binding_digest,
        context_bindings=context_refs,
        runtime_reviewed_execution_binding=runtime_binding,
        runtime_model_run_id=diagnostic.get("model_run_id"),
        runtime_artifact_identity=diagnostic.get("artifact_identity"),
        prepared_at=prepared_at,
        expires_at=health.expires_at,
        bounded_tool_request_binding=invocation.bounded_binding,
        _factory_seal=_DISCLOSURE_FACTORY_SEAL,
    )


def _validate_disclosure_binding(
    disclosure: ExternalSendDisclosure,
    selection: ProviderSelection,
    request: CognitiveModelRequest,
    reservation: ModelCallReservation,
    *,
    budget_state: ModelBudgetState | None = None,
) -> tuple[dict[str, Any], ...]:
    if not isinstance(disclosure, ExternalSendDisclosure):
        _fail("typed_external_send_disclosure_required")
    records = _validate_typed_contracts(
        selection,
        request,
        reservation,
        budget_state=budget_state,
    )
    now = _timestamp_value(
        _utc_now(),
        "invalid_external_send_disclosure_evaluation_time",
    )
    if now >= _timestamp_value(
        disclosure.expires_at,
        "invalid_external_send_disclosure_expires_at",
    ):
        _fail("external_send_disclosure_expired")
    context_refs = tuple(
        {
            "source_system": record["source_system"],
            "binding_id": record["binding_id"],
            "binding_digest": record["binding_digest"],
            "source_digest": record["source_digest"],
            "freshness": record["freshness"],
            "selected_items": record["selected_items"],
        }
        for record in records
    )
    invocation = _build_runtime_invocation(request, records)
    if (
        disclosure.provider_selection_digest != selection.selection_digest
        or disclosure.cognitive_request_digest != request.request_digest
        or disclosure.reservation_id != reservation.reservation_id
        or disclosure.reservation_digest != reservation_digest(reservation)
        or disclosure.provider_binding_digest
        != request.provider_binding.binding_digest
        or disclosure.context_bindings
        != tuple(_freeze(item) for item in context_refs)
        or disclosure.question != invocation.question
        or disclosure.budget_profile != invocation.budget_profile
        or disclosure.bounded_tool_request_binding
        != (
            None
            if invocation.bounded_binding is None
            else _freeze(invocation.bounded_binding)
        )
        or disclosure.disclosure_digest
        != canonical_digest(disclosure.identity_payload())
    ):
        _fail("external_send_disclosure_binding_mismatch")
    return records


def _build_preview(
    disclosure: ExternalSendDisclosure,
    selection: ProviderSelection,
    request: CognitiveModelRequest,
    reservation: ModelCallReservation,
    *,
    consent: ExternalSendConsent,
    budget_state: ModelBudgetState | None = None,
) -> CognitiveRuntimePreview:
    _validate_disclosure_binding(
        disclosure,
        selection,
        request,
        reservation,
        budget_state=budget_state,
    )
    if not isinstance(consent, ExternalSendConsent):
        _fail("typed_external_send_consent_required")
    try:
        consent.validate_binding(
            selection,
            request,
            reservation,
            **disclosure.consent_binding(),
            evaluated_at=_utc_now(),
        )
    except ExternalSendConsentError as exc:
        _fail(exc.reason_code)
    return CognitiveRuntimePreview(
        question=disclosure.question,
        budget_profile=disclosure.budget_profile,
        request_id=disclosure.request_id,
        mutation_id=disclosure.mutation_id,
        provider_selection_digest=disclosure.provider_selection_digest,
        cognitive_request_digest=disclosure.cognitive_request_digest,
        reservation_id=disclosure.reservation_id,
        reservation_digest=disclosure.reservation_digest,
        provider_binding_digest=disclosure.provider_binding_digest,
        external_send_consent_digest=consent.consent_digest,
        runtime_disclosure_digest=disclosure.disclosure_digest,
        runtime_disclosure_prepared_at=disclosure.prepared_at,
        runtime_disclosure_expires_at=disclosure.expires_at,
        context_bindings=disclosure.context_bindings,
        runtime_reviewed_execution_binding=(
            disclosure.runtime_reviewed_execution_binding
        ),
        runtime_model_run_id=disclosure.runtime_model_run_id,
        runtime_artifact_identity=disclosure.runtime_artifact_identity,
        bounded_tool_request_binding=(
            disclosure.bounded_tool_request_binding
        ),
        _factory_seal=_PREVIEW_FACTORY_SEAL,
    )


def _validate_exact_preview(
    preview: CognitiveRuntimePreview,
    selection: ProviderSelection,
    request: CognitiveModelRequest,
    reservation: ModelCallReservation,
    consent: ExternalSendConsent,
    *,
    budget_state: ModelBudgetState | None = None,
) -> None:
    if not isinstance(preview, CognitiveRuntimePreview):
        _fail("typed_cognitive_runtime_preview_required")
    disclosure = ExternalSendDisclosure(
        question=preview.question,
        budget_profile=preview.budget_profile,
        request_id=preview.request_id,
        mutation_id=preview.mutation_id,
        provider_selection_digest=preview.provider_selection_digest,
        cognitive_request_digest=preview.cognitive_request_digest,
        reservation_id=preview.reservation_id,
        reservation_digest=preview.reservation_digest,
        provider_binding_digest=preview.provider_binding_digest,
        context_bindings=preview.context_bindings,
        runtime_reviewed_execution_binding=(
            preview.runtime_reviewed_execution_binding
        ),
        runtime_model_run_id=preview.runtime_model_run_id,
        runtime_artifact_identity=preview.runtime_artifact_identity,
        prepared_at=preview.runtime_disclosure_prepared_at,
        expires_at=preview.runtime_disclosure_expires_at,
        bounded_tool_request_binding=preview.bounded_tool_request_binding,
        disclosure_digest=preview.runtime_disclosure_digest,
        _factory_seal=_DISCLOSURE_FACTORY_SEAL,
    )
    records = _validate_disclosure_binding(
        disclosure,
        selection,
        request,
        reservation,
        budget_state=budget_state,
    )
    if not isinstance(consent, ExternalSendConsent):
        _fail("typed_external_send_consent_required")
    try:
        consent.validate_binding(
            selection,
            request,
            reservation,
            **disclosure.consent_binding(),
            evaluated_at=_utc_now(),
        )
    except ExternalSendConsentError as exc:
        _fail(exc.reason_code)
    invocation = _build_runtime_invocation(request, records)
    if (
        preview.provider_selection_digest != selection.selection_digest
        or preview.cognitive_request_digest != request.request_digest
        or preview.reservation_id != reservation.reservation_id
        or preview.reservation_digest != reservation_digest(reservation)
        or preview.provider_binding_digest
        != request.provider_binding.binding_digest
        or preview.external_send_consent_digest != consent.consent_digest
        or preview.runtime_disclosure_digest != disclosure.disclosure_digest
        or preview.question != invocation.question
        or preview.budget_profile != invocation.budget_profile
        or preview.bounded_tool_request_binding
        != (
            None
            if invocation.bounded_binding is None
            else _freeze(invocation.bounded_binding)
        )
        or preview.bridge_digest != canonical_digest(preview.identity_payload())
    ):
        _fail("cognitive_runtime_preview_drift")


def _execute(
    runtime: Any,
    preview: CognitiveRuntimePreview,
    selection: ProviderSelection,
    request: CognitiveModelRequest,
    reservation: ModelCallReservation,
    *,
    consent: ExternalSendConsent,
    confirmed: bool,
    authority: Any,
    mock_expected: bool,
    budget_state: ModelBudgetState | None = None,
) -> CognitiveRuntimeExecutionReference:
    _validate_exact_preview(
        preview,
        selection,
        request,
        reservation,
        consent,
        budget_state=budget_state,
    )
    if confirmed is not True:
        _fail("explicit_cognitive_runtime_confirmation_required")
    records = _context_records(request)
    invocation = _build_runtime_invocation(request, records)
    try:
        common = {
            "request_id": preview.request_id,
            "mutation_id": preview.mutation_id,
            "expected_bindings": _materialize(
                preview.runtime_reviewed_execution_binding
            ),
            "confirmed": True,
            "authority": authority,
        }
        if invocation.bounded_request is None:
            envelope = runtime.execute(
                invocation.question,
                budget_profile=invocation.budget_profile,
                **common,
            )
        else:
            envelope = runtime.execute_bounded_tool(
                invocation.bounded_request,
                **common,
            )
    except RuntimePreviewMismatch:
        _fail("runtime_preview_drift")
    except ProductionRuntimeExecutionError as exc:
        _fail(exc.reason_code)

    if not isinstance(envelope, dict) or not isinstance(
        envelope.get("result"), dict
    ):
        _fail("runtime_execution_envelope_invalid")
    result = envelope["result"]
    transport = envelope.get("transport")
    if not isinstance(transport, dict):
        _fail("runtime_transport_reference_missing")
    if (
        transport.get("automatic_retry") is not False
        or transport.get("adapter_call_count_this_request") not in {0, 1}
        or transport.get("model_call_count_for_run") not in {0, 1}
        or not isinstance(transport.get("exactly_one_call"), bool)
        or transport.get("exactly_one_call")
        is not (transport.get("model_call_count_for_run") == 1)
        or transport.get("mock_transport") is not mock_expected
        or not isinstance(transport.get("network_request_count"), int)
        or isinstance(transport.get("network_request_count"), bool)
        or transport.get("network_request_count") not in {0, 1}
        or not isinstance(transport.get("credential_read"), bool)
    ):
        _fail("runtime_one_shot_transport_violation")
    if mock_expected:
        if (
            transport["network_request_count"] != 0
            or transport["credential_read"]
        ):
            _fail("mock_runtime_transport_boundary_violation")
    elif (
        transport["network_request_count"]
        != transport["adapter_call_count_this_request"]
        or transport["credential_read"]
        is not (transport["adapter_call_count_this_request"] == 1)
    ):
        _fail("production_runtime_transport_boundary_violation")
    model_run_id = result.get("model_run_id")
    if model_run_id != preview.runtime_model_run_id:
        _fail("runtime_model_run_binding_mismatch")
    try:
        authoritative = runtime.get_run(model_run_id)
    except ProductionRuntimeExecutionError as exc:
        _fail(exc.reason_code)
    if canonical_json(authoritative) != canonical_json(result):
        _fail("runtime_execution_reference_mismatch")
    if (
        result.get("request_id") != preview.request_id
        or result.get("mutation_id") != preview.mutation_id
        or result.get("provider") != PRODUCTION_PROVIDER_ID
        or result.get("model") != PRODUCTION_MODEL_ID
    ):
        _fail("runtime_execution_provider_binding_mismatch")
    artifact = result.get("request_artifact")
    artifact_identity = (
        artifact.get("artifact_identity") if isinstance(artifact, dict) else None
    )
    if artifact_identity != preview.runtime_artifact_identity:
        _fail("runtime_artifact_binding_mismatch")

    status = result.get("status")
    runtime_receipt_identity = result.get("receipt_identity")
    if status in {"succeeded", "failed"}:
        if (
            not isinstance(runtime_receipt_identity, str)
            or runtime_receipt_identity != receipt_identity(result)
        ):
            _fail("runtime_receipt_identity_mismatch")
    elif runtime_receipt_identity is not None:
        _fail("runtime_nonterminal_receipt_forbidden")

    usage = result.get("usage")
    usage_digest: str | None = None
    token_budget_verified = False
    if status == "succeeded":
        if (
            not isinstance(usage, dict)
            or set(usage)
            != {"prompt_tokens", "completion_tokens", "total_tokens"}
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in usage.values()
            )
            or usage["total_tokens"]
            != usage["prompt_tokens"] + usage["completion_tokens"]
        ):
            _fail("runtime_usage_reference_invalid")
        if (
            usage["prompt_tokens"] > reservation.input_tokens
            or usage["completion_tokens"] > reservation.output_token_ceiling
            or usage["total_tokens"]
            > reservation.input_tokens + reservation.output_token_ceiling
        ):
            _fail("runtime_usage_exceeds_reservation")
        usage_digest = canonical_digest(usage)
        token_budget_verified = True
    elif usage is not None:
        _fail("runtime_failed_usage_reference_invalid")

    adapter_calls = transport["adapter_call_count_this_request"]
    model_calls = transport["model_call_count_for_run"]
    return CognitiveRuntimeExecutionReference(
        bridge_digest=preview.bridge_digest,
        runtime_model_run_id=model_run_id,
        runtime_receipt_identity=runtime_receipt_identity,
        runtime_artifact_identity=artifact_identity,
        runtime_usage_digest=usage_digest,
        status=status,
        execution_phase=result.get("execution_phase"),
        adapter_call_count_this_request=adapter_calls,
        model_call_count_for_run=model_calls,
        exactly_one_call=transport["exactly_one_call"],
        mock_transport=transport["mock_transport"],
        network_request_count=transport["network_request_count"],
        credential_read=transport["credential_read"],
        replayed=adapter_calls == 0 and model_calls == 1,
        token_budget_verified=token_budget_verified,
    )


class ProductionCognitiveRuntimeBridge:
    """Fixed production facade; dependency and path injection are impossible."""

    __slots__ = ()

    def prepare_disclosure(
        self,
        selection: ProviderSelection,
        request: CognitiveModelRequest,
        reservation: ModelCallReservation,
        *,
        request_id: str,
        mutation_id: str,
        budget_state: ModelBudgetState | None = None,
    ) -> ExternalSendDisclosure:
        return _prepare_disclosure(
            ProductionRuntimeExecutionService(),
            selection,
            request,
            reservation,
            request_id=request_id,
            mutation_id=mutation_id,
            budget_state=budget_state,
        )

    def preview(
        self,
        disclosure: ExternalSendDisclosure,
        selection: ProviderSelection,
        request: CognitiveModelRequest,
        reservation: ModelCallReservation,
        *,
        consent: ExternalSendConsent | None = None,
        budget_state: ModelBudgetState | None = None,
    ) -> CognitiveRuntimePreview:
        return _build_preview(
            disclosure,
            selection,
            request,
            reservation,
            consent=consent,
            budget_state=budget_state,
        )

    def execute(
        self,
        preview: CognitiveRuntimePreview,
        selection: ProviderSelection,
        request: CognitiveModelRequest,
        reservation: ModelCallReservation,
        *,
        consent: ExternalSendConsent | None = None,
        confirmed: bool,
        authority: ProductionRuntimeExecutionSession,
        budget_state: ModelBudgetState | None = None,
    ) -> CognitiveRuntimeExecutionReference:
        if not is_trusted_production_session(authority):
            _fail("production_runtime_authority_required")
        return _execute(
            ProductionRuntimeExecutionService(),
            preview,
            selection,
            request,
            reservation,
            consent=consent,
            confirmed=confirmed,
            authority=authority,
            mock_expected=False,
            budget_state=budget_state,
        )

    def read_model_observation(
        self,
        preview: CognitiveRuntimePreview,
        request: CognitiveModelRequest,
        reservation: ModelCallReservation,
        *,
        budget_state: ModelBudgetState,
    ) -> CognitiveRuntimeModelObservation:
        return _read_model_observation(
            ProductionRuntimeExecutionService(),
            preview,
            request,
            reservation,
            budget_state=budget_state,
        )

    def reconcile_model_run(
        self,
        preview: CognitiveRuntimePreview,
        request: CognitiveModelRequest,
        reservation: ModelCallReservation,
        *,
        budget_state: ModelBudgetState,
        capability_registry: CapabilityRegistry | None = None,
        seen_output_digests: Iterable[str] = (),
        seen_request_digests: Iterable[str] = (),
    ) -> CognitiveRuntimeReconciliation:
        return _reconcile_model_run(
            ProductionRuntimeExecutionService(),
            preview,
            request,
            reservation,
            budget_state=budget_state,
            capability_registry=capability_registry,
            seen_output_digests=seen_output_digests,
            seen_request_digests=seen_request_digests,
        )


class _ScenarioRuntimeService:
    """Explicit test-only fault boundary around the public v8 mock service."""

    def __init__(
        self,
        runtime: MockRuntimeExecutionService,
        scenario: str,
    ) -> None:
        self._runtime = runtime
        self.scenario = scenario
        self.execution_session = runtime.execution_session
        self.preview_calls = 0
        self.execute_calls = 0
        self._overrides: dict[str, dict[str, Any]] = {}

    def preview(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.preview_calls += 1
        return self._runtime.preview(*args, **kwargs)

    def preview_bounded_tool(
        self, *args: Any, **kwargs: Any
    ) -> dict[str, Any]:
        self.preview_calls += 1
        return self._runtime.preview_bounded_tool(*args, **kwargs)

    def execute(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.execute_calls += 1
        if self.scenario == "provider_failure":
            raise ProductionRuntimeExecutionError("mock_provider_failure")
        if self.scenario == "preview_drift":
            changed = dict(kwargs)
            changed["expected_bindings"] = {}
            return self._runtime.execute(*args, **changed)
        envelope = self._runtime.execute(*args, **kwargs)
        if self.scenario == "usage_over_budget":
            changed = deepcopy(envelope)
            result = changed["result"]
            usage = dict(result["usage"])
            usage["prompt_tokens"] = 10**9
            usage["total_tokens"] = (
                usage["prompt_tokens"] + usage["completion_tokens"]
            )
            result["usage"] = usage
            result["receipt_identity"] = receipt_identity(result)
            self._overrides[result["model_run_id"]] = deepcopy(result)
            return changed
        return envelope

    def execute_bounded_tool(
        self, *args: Any, **kwargs: Any
    ) -> dict[str, Any]:
        self.execute_calls += 1
        if self.scenario == "provider_failure":
            raise ProductionRuntimeExecutionError("mock_provider_failure")
        if self.scenario == "preview_drift":
            changed = dict(kwargs)
            changed["expected_bindings"] = {}
            return self._runtime.execute_bounded_tool(*args, **changed)
        envelope = self._runtime.execute_bounded_tool(*args, **kwargs)
        if self.scenario == "usage_over_budget":
            changed = deepcopy(envelope)
            result = changed["result"]
            usage = dict(result["usage"])
            usage["prompt_tokens"] = 10**9
            usage["total_tokens"] = (
                usage["prompt_tokens"] + usage["completion_tokens"]
            )
            result["usage"] = usage
            result["receipt_identity"] = receipt_identity(result)
            self._overrides[result["model_run_id"]] = deepcopy(result)
            return changed
        return envelope

    def get_run(self, model_run_id: str) -> dict[str, Any]:
        override = self._overrides.get(model_run_id)
        if override is not None:
            return deepcopy(override)
        return self._runtime.get_run(model_run_id)


class MockCognitiveRuntimeBridge:
    """Explicit temporary-sidecar facade; never accepted by production code."""

    __slots__ = ("_runtime", "execution_session")

    def __init__(self, runtime: _ScenarioRuntimeService) -> None:
        self._runtime = runtime
        self.execution_session = runtime.execution_session

    @property
    def diagnostics(self) -> dict[str, Any]:
        return {
            "mock": True,
            "scenario": self._runtime.scenario,
            "preview_calls": self._runtime.preview_calls,
            "execute_calls": self._runtime.execute_calls,
            "automatic_retry": False,
        }

    def prepare_disclosure(
        self,
        selection: ProviderSelection,
        request: CognitiveModelRequest,
        reservation: ModelCallReservation,
        *,
        request_id: str,
        mutation_id: str,
        budget_state: ModelBudgetState | None = None,
    ) -> ExternalSendDisclosure:
        return _prepare_disclosure(
            self._runtime,
            selection,
            request,
            reservation,
            request_id=request_id,
            mutation_id=mutation_id,
            budget_state=budget_state,
        )

    def preview(
        self,
        disclosure: ExternalSendDisclosure,
        selection: ProviderSelection,
        request: CognitiveModelRequest,
        reservation: ModelCallReservation,
        *,
        consent: ExternalSendConsent | None = None,
        budget_state: ModelBudgetState | None = None,
    ) -> CognitiveRuntimePreview:
        return _build_preview(
            disclosure,
            selection,
            request,
            reservation,
            consent=consent,
            budget_state=budget_state,
        )

    def execute(
        self,
        preview: CognitiveRuntimePreview,
        selection: ProviderSelection,
        request: CognitiveModelRequest,
        reservation: ModelCallReservation,
        *,
        consent: ExternalSendConsent | None = None,
        confirmed: bool,
        authority: Any,
        budget_state: ModelBudgetState | None = None,
    ) -> CognitiveRuntimeExecutionReference:
        if authority is not self.execution_session:
            _fail("mock_runtime_authority_required")
        return _execute(
            self._runtime,
            preview,
            selection,
            request,
            reservation,
            consent=consent,
            confirmed=confirmed,
            authority=authority,
            mock_expected=True,
            budget_state=budget_state,
        )

    def read_model_observation(
        self,
        preview: CognitiveRuntimePreview,
        request: CognitiveModelRequest,
        reservation: ModelCallReservation,
        *,
        budget_state: ModelBudgetState,
    ) -> CognitiveRuntimeModelObservation:
        return _read_model_observation(
            self._runtime,
            preview,
            request,
            reservation,
            budget_state=budget_state,
        )

    def reconcile_model_run(
        self,
        preview: CognitiveRuntimePreview,
        request: CognitiveModelRequest,
        reservation: ModelCallReservation,
        *,
        budget_state: ModelBudgetState,
        capability_registry: CapabilityRegistry | None = None,
        seen_output_digests: Iterable[str] = (),
        seen_request_digests: Iterable[str] = (),
    ) -> CognitiveRuntimeReconciliation:
        return _reconcile_model_run(
            self._runtime,
            preview,
            request,
            reservation,
            budget_state=budget_state,
            capability_registry=capability_registry,
            seen_output_digests=seen_output_digests,
            seen_request_digests=seen_request_digests,
        )


def create_mock_cognitive_runtime_bridge(
    sidecar_path: str | Path,
    *,
    scenario: str = "success",
) -> MockCognitiveRuntimeBridge:
    """Create the only dependency-injectable bridge, bound to a temp sidecar."""

    if scenario not in _MOCK_SCENARIOS:
        _fail("invalid_mock_runtime_scenario")
    runtime = create_mock_runtime_execution_service(sidecar_path)
    return MockCognitiveRuntimeBridge(_ScenarioRuntimeService(runtime, scenario))


__all__ = [
    "BRIDGE_CONTRACT_VERSION",
    "BRIDGE_RECONCILIATION_VERSION",
    "CognitiveRuntimeBridgeError",
    "CognitiveRuntimeExecutionReference",
    "CognitiveRuntimeModelObservation",
    "CognitiveRuntimePreview",
    "CognitiveRuntimeReconciliation",
    "CognitiveRuntimeRecoveryRequired",
    "EXTERNAL_SEND_DISCLOSURE_VERSION",
    "ExternalSendDisclosure",
    "MockCognitiveRuntimeBridge",
    "MAX_BOUNDED_MODEL_CALLS",
    "PRODUCTION_FIXED_OUTPUT_TOKENS",
    "PRODUCTION_BUDGET_PROFILE",
    "PRODUCTION_MODEL_ID",
    "PRODUCTION_PROVIDER_ID",
    "PRODUCTION_PROVIDER_PROFILE_DIGEST",
    "PRODUCTION_PROVIDER_PROFILE_VERSION",
    "ProductionCognitiveRuntimeBridge",
    "REQUIRED_CONTEXT_SOURCES",
    "create_mock_cognitive_runtime_bridge",
]
