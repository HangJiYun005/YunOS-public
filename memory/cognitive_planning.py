from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping, Sequence

from memory.cognitive_capability import (
    CapabilityProfile,
    CapabilityRegistry,
    CognitiveCapabilityError,
    build_suggested_capability_registry,
)
from memory.cognitive_objective import (
    AGENCY_LEVELS,
    COGNITIVE_SCHEMA_GENERATION,
    RISK_CLASSES,
    ObjectiveContract,
)
from memory.operational_state_models import (
    OperationalStateError,
    SourceAnchor,
    canonical_digest,
    canonical_json,
    make_identity,
    normalize_timestamp,
    validate_identity,
    validate_sha256,
)


PLAN_GRAPH_VERSION = 1
NODE_KINDS = (
    "action",
    "decision",
    "human_gate",
    "rollback",
    "alternative",
    "verification",
)
EDGE_KINDS = (
    "sequence",
    "parallel",
    "decision",
    "failure",
    "rollback",
    "alternative",
    "resume",
)
AUTHORITY_REQUIREMENTS = (
    "none",
    "read",
    "prepare",
    "explicit_act",
    "human_gate",
)
NODE_STATES = (
    "proposed",
    "ready",
    "running",
    "waiting_authority",
    "waiting_external",
    "verifying",
    "completed",
    "failed",
    "cancelled",
    "omitted",
    "rollback_required",
    "rolled_back",
)
AUTHORITY_IMPACTS = ("none", "narrows_scope", "requires_new_authority")
REVISION_ACTORS = ("user", "agent", "system")

_RISK_RANK = {value: rank for rank, value in enumerate(RISK_CLASSES)}
_AGENCY_RANK = {value: rank for rank, value in enumerate(AGENCY_LEVELS)}
_AUTHORITY_AGENCY = {
    "none": "consult",
    "read": "consult",
    "prepare": "prepare",
    "explicit_act": "act",
    "human_gate": "act",
}
_AUTHORITY_RANK = {
    "none": 0,
    "read": 1,
    "prepare": 2,
    "explicit_act": 3,
    "human_gate": 4,
}
_MAX_TEXT_ITEMS = 256
_ABSOLUTE_MAX_NODES = 512
_ABSOLUTE_MAX_EDGES = 2048
_ABSOLUTE_MAX_REVISIONS = 128
_ABSOLUTE_MAX_BYTES = 4 * 1024 * 1024
_ABSOLUTE_MAX_EVIDENCE = 2048
_ABSOLUTE_MAX_TOTAL_TIMEOUT_MS = 86_400_000

_SECRET_LITERAL_PATTERNS = (
    re.compile(r"(?i)\bsk-(?:proj-)?[a-z0-9_-]{12,}\b"),
    re.compile(r"(?i)\bgh[pousr]_[a-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]{10,}\b"),
    re.compile(
        r"(?i)(?:^|[^a-z0-9_])[\"']?(?:[a-z0-9]+[_-])*"
        r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|"
        r"client[_-]?secret|password|passwd|authorization|token|secret)"
        r"[\"']?\s*[:=]\s*[\"']?(?:bearer\s+|basic\s+)?"
        r"[^\s\"'},;]{8,}"
    ),
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(
        r"\beyJ[a-zA-Z0-9_-]{8,}\.[a-zA-Z0-9_-]{8,}"
        r"\.[a-zA-Z0-9_-]{8,}\b"
    ),
)
_PROMPT_INJECTION_PATTERNS = (
    re.compile(
        r"(?i)\b(?:ignore|disregard|override)\s+(?:all\s+|any\s+|the\s+)?"
        r"(?:previous|prior|system|developer)\s+(?:instructions?|messages?|rules?)"
    ),
    re.compile(r"(?i)\byou\s+are\s+now\s+(?:an?|the)\b"),
    re.compile(r"(?i)\breveal\s+(?:the\s+)?(?:system|developer)\s+prompt\b"),
    re.compile(r"<\|(?:im_start|im_end|system|developer)\|>", re.IGNORECASE),
    re.compile(r"(?i)\b(?:jailbreak|prompt\s+leak)\s*:\s*\S"),
)
_DENIED_CAPABILITY_RE = re.compile(
    r"(?i)(?:^|[_.:-])(?:git[_.:-])?(?:merge|tag|push)(?:$|[_.:-])"
)
_DENIED_VERSION_RE = re.compile(
    r"(?i)(?:^|[_.:-])(?:create|start|open|begin)?[_.:-]?"
    r"(?:next[_.:-]?version|v10[_.:-]?g|g0|v10[_.:-]?1|v11)(?:$|[_.:-])"
)
_DENIED_ACTION_RE = re.compile(
    r"(?i)^\s*(?:please\s+)?(?:run\s+)?(?:git\s+)?"
    r"(?:merge|tag|push)\b"
)
_DENIED_NEXT_VERSION_ACTION_RE = re.compile(
    r"(?i)^\s*(?:create|start|open|begin|implement)\s+"
    r"(?:the\s+)?(?:next\s+version|v10-g|g0|v10\.1|v11)\b"
)


class PlanningError(OperationalStateError):
    """Fail-closed error for immutable v10-B Plan Graph contracts."""


def _fail(reason_code: str) -> None:
    raise PlanningError(reason_code)


def _require_bool(value: Any, reason_code: str) -> bool:
    if not isinstance(value, bool):
        _fail(reason_code)
    return value


def _require_int(
    value: Any,
    *,
    minimum: int,
    maximum: int | None,
    reason_code: str,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        _fail(reason_code)
    if maximum is not None and value > maximum:
        _fail(reason_code)
    return value


def _validate_enum(value: Any, allowed: Sequence[str], reason_code: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        _fail(reason_code)
    return value


def _reject_unsafe_text(value: str) -> None:
    if any(pattern.search(value) for pattern in _SECRET_LITERAL_PATTERNS):
        _fail("plan_secret_literal_forbidden")
    if any(pattern.search(value) for pattern in _PROMPT_INJECTION_PATTERNS):
        _fail("plan_prompt_injection_forbidden")


def _normalize_text(
    value: Any,
    *,
    reason_code: str,
    maximum: int = 4096,
) -> str:
    if not isinstance(value, str):
        _fail(reason_code)
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized or len(normalized) > maximum or "\x00" in normalized:
        _fail(reason_code)
    _reject_unsafe_text(normalized)
    return normalized


def _normalize_optional_text(
    value: Any,
    *,
    reason_code: str,
    maximum: int = 4096,
) -> str | None:
    if value is None:
        return None
    return _normalize_text(value, reason_code=reason_code, maximum=maximum)


def _normalize_strings(
    values: Iterable[str],
    *,
    reason_code: str,
    maximum: int = 4096,
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        _fail(reason_code)
    materialized = list(values)
    if len(materialized) > _MAX_TEXT_ITEMS:
        _fail("plan_collection_budget_exceeded")
    return tuple(
        sorted(
            {
                _normalize_text(
                    item,
                    reason_code=reason_code,
                    maximum=maximum,
                )
                for item in materialized
            }
        )
    )


def _normalize_identities(
    values: Iterable[str],
    *,
    reason_code: str,
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        _fail(reason_code)
    materialized = list(values)
    if len(materialized) > _MAX_TEXT_ITEMS:
        _fail("plan_collection_budget_exceeded")
    normalized: set[str] = set()
    for value in materialized:
        if isinstance(value, str):
            _reject_unsafe_text(value)
        try:
            normalized.add(validate_identity(value, reason_code))
        except OperationalStateError as exc:
            raise PlanningError(exc.reason_code) from None
    return tuple(sorted(normalized))


def _validate_identity(value: Any, reason_code: str) -> str:
    if isinstance(value, str):
        _reject_unsafe_text(value)
    try:
        return validate_identity(value, reason_code)
    except OperationalStateError as exc:
        raise PlanningError(exc.reason_code) from None


def _validate_digest(value: Any, reason_code: str) -> str:
    try:
        return validate_sha256(value, reason_code)
    except OperationalStateError as exc:
        raise PlanningError(exc.reason_code) from None


def _normalize_timestamp(value: Any, reason_code: str) -> str:
    try:
        return normalize_timestamp(value)
    except OperationalStateError:
        _fail(reason_code)


def _validate_scope(scope: str, reason_code: str) -> str:
    normalized = _normalize_text(scope, reason_code=reason_code, maximum=1024)
    if any(character in normalized for character in ("*", "?", "[", "]")):
        _fail("ambiguous_plan_scope_forbidden")
    if "\n" in normalized or "\r" in normalized:
        _fail(reason_code)
    segments = normalized.replace("\\", "/").split("/")
    if ".." in segments:
        _fail("plan_scope_traversal_forbidden")
    return normalized


def _normalize_scopes(
    values: Iterable[str],
    *,
    reason_code: str,
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        _fail(reason_code)
    materialized = list(values)
    if len(materialized) > _MAX_TEXT_ITEMS:
        _fail("plan_collection_budget_exceeded")
    return tuple(sorted({_validate_scope(item, reason_code) for item in materialized}))


def _scope_key(value: str) -> str:
    return value.replace("\\", "/").rstrip("/").casefold()


def _scope_is_within(scope: str, roots: Sequence[str]) -> bool:
    candidate = _scope_key(scope)
    for root in roots:
        boundary = _scope_key(root)
        if candidate == boundary or candidate.startswith(f"{boundary}/"):
            return True
    return False


def _coerce_anchor(value: SourceAnchor | Mapping[str, Any]) -> SourceAnchor:
    try:
        anchor = (
            value
            if isinstance(value, SourceAnchor)
            else SourceAnchor.from_dict(value)
        )
    except (OperationalStateError, TypeError):
        _fail("invalid_plan_evidence")
    _reject_unsafe_text(anchor.anchor_id)
    _reject_unsafe_text(anchor.locator)
    return anchor


def _coerce_node(value: PlanNode | Mapping[str, Any]) -> PlanNode:
    return value if isinstance(value, PlanNode) else PlanNode.from_dict(value)


def _coerce_edge(value: PlanEdge | Mapping[str, Any]) -> PlanEdge:
    return value if isinstance(value, PlanEdge) else PlanEdge.from_dict(value)


def _coerce_capability_requirement(
    value: CapabilityRequirement | Mapping[str, Any],
) -> CapabilityRequirement:
    return (
        value
        if isinstance(value, CapabilityRequirement)
        else CapabilityRequirement.from_dict(value)
    )


@dataclass(frozen=True)
class PlanningBudget:
    maximum_nodes: int = 128
    maximum_edges: int = 512
    maximum_revisions: int = 64
    maximum_canonical_bytes: int = 1_048_576
    maximum_evidence_items: int = 512
    maximum_total_timeout_ms: int = 43_200_000

    def __post_init__(self) -> None:
        for field, absolute in (
            ("maximum_nodes", _ABSOLUTE_MAX_NODES),
            ("maximum_edges", _ABSOLUTE_MAX_EDGES),
            ("maximum_revisions", _ABSOLUTE_MAX_REVISIONS),
            ("maximum_canonical_bytes", _ABSOLUTE_MAX_BYTES),
            ("maximum_evidence_items", _ABSOLUTE_MAX_EVIDENCE),
            ("maximum_total_timeout_ms", _ABSOLUTE_MAX_TOTAL_TIMEOUT_MS),
        ):
            _require_int(
                getattr(self, field),
                minimum=1,
                maximum=absolute,
                reason_code=f"invalid_{field}",
            )


DEFAULT_PLANNING_BUDGET = PlanningBudget()


@dataclass(frozen=True)
class RetryPolicy:
    maximum_attempts: int = 1
    equivalent_retry_limit: int = 0
    backoff_ms: int = 0
    retryable_failures: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_int(
            self.maximum_attempts,
            minimum=1,
            maximum=2,
            reason_code="invalid_retry_maximum_attempts",
        )
        _require_int(
            self.equivalent_retry_limit,
            minimum=0,
            maximum=1,
            reason_code="invalid_equivalent_retry_limit",
        )
        if self.equivalent_retry_limit >= self.maximum_attempts:
            _fail("retry_policy_attempt_budget_mismatch")
        _require_int(
            self.backoff_ms,
            minimum=0,
            maximum=60_000,
            reason_code="invalid_retry_backoff_ms",
        )
        object.__setattr__(
            self,
            "retryable_failures",
            _normalize_strings(
                self.retryable_failures,
                reason_code="invalid_retryable_failure",
                maximum=256,
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "maximum_attempts": self.maximum_attempts,
            "equivalent_retry_limit": self.equivalent_retry_limit,
            "backoff_ms": self.backoff_ms,
            "retryable_failures": list(self.retryable_failures),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RetryPolicy:
        expected = {
            "maximum_attempts",
            "equivalent_retry_limit",
            "backoff_ms",
            "retryable_failures",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            _fail("invalid_retry_policy")
        payload = dict(value)
        if not isinstance(payload["retryable_failures"], list):
            _fail("invalid_retry_policy")
        payload["retryable_failures"] = tuple(payload["retryable_failures"])
        return cls(**payload)


@dataclass(frozen=True)
class CapabilityRequirement:
    capability_id: str
    capability_version: int
    capability_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "capability_id",
            _validate_identity(
                self.capability_id, "invalid_plan_capability_id"
            ),
        )
        _require_int(
            self.capability_version,
            minimum=1,
            maximum=1_000_000,
            reason_code="invalid_plan_capability_version",
        )
        _validate_digest(
            self.capability_digest, "invalid_plan_capability_digest"
        )
        if _DENIED_CAPABILITY_RE.search(self.capability_id):
            _fail("plan_release_operation_denied")
        if _DENIED_VERSION_RE.search(self.capability_id):
            _fail("plan_next_version_denied")

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "capability_version": self.capability_version,
            "capability_digest": self.capability_digest,
        }

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any]
    ) -> CapabilityRequirement:
        expected = {
            "capability_id",
            "capability_version",
            "capability_digest",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            _fail("invalid_plan_capability_requirement")
        return cls(**dict(value))


@dataclass(frozen=True)
class PlanNode:
    node_id: str
    objective_id: str
    title: str
    purpose: str
    dependencies: tuple[str, ...] = ()
    node_kind: str = "action"
    capability_requirements: tuple[CapabilityRequirement, ...] = ()
    read_scope: tuple[str, ...] = ()
    write_scope: tuple[str, ...] = ()
    risk: str = "low"
    reversible: bool = True
    authority_requirement: str = "none"
    success_criteria: tuple[str, ...] = ()
    verification: tuple[str, ...] = ()
    rollback: tuple[str, ...] = ()
    timeout_ms: int = 60_000
    retry_policy: RetryPolicy = RetryPolicy()
    state: str = "proposed"
    evidence: tuple[SourceAnchor, ...] = ()
    omitted_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "node_id", _validate_identity(self.node_id, "invalid_plan_node_id")
        )
        object.__setattr__(
            self,
            "objective_id",
            _validate_identity(self.objective_id, "invalid_plan_objective_id"),
        )
        object.__setattr__(
            self,
            "title",
            _normalize_text(self.title, reason_code="invalid_plan_node_title"),
        )
        object.__setattr__(
            self,
            "purpose",
            _normalize_text(self.purpose, reason_code="invalid_plan_node_purpose"),
        )
        if _DENIED_ACTION_RE.search(self.title) or _DENIED_ACTION_RE.search(
            self.purpose
        ):
            _fail("plan_release_operation_denied")
        if _DENIED_NEXT_VERSION_ACTION_RE.search(
            self.title
        ) or _DENIED_NEXT_VERSION_ACTION_RE.search(self.purpose):
            _fail("plan_next_version_denied")
        object.__setattr__(
            self,
            "dependencies",
            _normalize_identities(
                self.dependencies, reason_code="invalid_plan_dependency"
            ),
        )
        if self.node_id in self.dependencies:
            _fail("plan_node_self_dependency")
        object.__setattr__(
            self,
            "node_kind",
            _validate_enum(self.node_kind, NODE_KINDS, "invalid_plan_node_kind"),
        )
        if isinstance(self.capability_requirements, (str, bytes)) or not isinstance(
            self.capability_requirements, Sequence
        ):
            _fail("invalid_plan_capability_requirement")
        requirements = tuple(
            sorted(
                (
                    _coerce_capability_requirement(item)
                    for item in self.capability_requirements
                ),
                key=lambda item: (
                    item.capability_id,
                    item.capability_version,
                    item.capability_digest,
                ),
            )
        )
        if len(
            {
                (item.capability_id, item.capability_version)
                for item in requirements
            }
        ) != len(requirements):
            _fail("duplicate_plan_capability_requirement")
        object.__setattr__(self, "capability_requirements", requirements)
        object.__setattr__(
            self,
            "read_scope",
            _normalize_scopes(
                self.read_scope, reason_code="invalid_plan_read_scope"
            ),
        )
        object.__setattr__(
            self,
            "write_scope",
            _normalize_scopes(
                self.write_scope, reason_code="invalid_plan_write_scope"
            ),
        )
        object.__setattr__(
            self,
            "risk",
            _validate_enum(self.risk, RISK_CLASSES, "invalid_plan_risk"),
        )
        _require_bool(self.reversible, "invalid_plan_reversible")
        object.__setattr__(
            self,
            "authority_requirement",
            _validate_enum(
                self.authority_requirement,
                AUTHORITY_REQUIREMENTS,
                "invalid_plan_authority_requirement",
            ),
        )
        object.__setattr__(
            self,
            "success_criteria",
            _normalize_strings(
                self.success_criteria,
                reason_code="invalid_plan_success_criterion",
            ),
        )
        if not self.success_criteria:
            _fail("plan_success_criteria_required")
        object.__setattr__(
            self,
            "verification",
            _normalize_strings(
                self.verification,
                reason_code="invalid_plan_verification",
            ),
        )
        if not self.verification:
            _fail("plan_verification_required")
        object.__setattr__(
            self,
            "rollback",
            _normalize_strings(
                self.rollback,
                reason_code="invalid_plan_rollback",
            ),
        )
        if self.write_scope and self.reversible and not self.rollback:
            _fail("reversible_write_requires_rollback")
        if not self.reversible and self.authority_requirement not in {
            "explicit_act",
            "human_gate",
        }:
            _fail("irreversible_node_requires_explicit_authority")
        _require_int(
            self.timeout_ms,
            minimum=1,
            maximum=1_200_000,
            reason_code="invalid_plan_timeout_ms",
        )
        if not isinstance(self.retry_policy, RetryPolicy):
            _fail("invalid_retry_policy")
        object.__setattr__(
            self,
            "state",
            _validate_enum(self.state, NODE_STATES, "invalid_plan_node_state"),
        )
        if isinstance(self.evidence, (str, bytes)) or not isinstance(
            self.evidence, Sequence
        ):
            _fail("invalid_plan_evidence")
        anchors = tuple(
            sorted(
                (_coerce_anchor(item) for item in self.evidence),
                key=lambda item: (
                    item.anchor_id,
                    item.evidence_digest,
                    item.observed_at,
                ),
            )
        )
        if len({item.anchor_id for item in anchors}) != len(anchors):
            _fail("duplicate_plan_evidence")
        object.__setattr__(self, "evidence", anchors)
        object.__setattr__(
            self,
            "omitted_reason",
            _normalize_optional_text(
                self.omitted_reason,
                reason_code="invalid_plan_omitted_reason",
            ),
        )
        if (self.state == "omitted") != (self.omitted_reason is not None):
            _fail("plan_omitted_reason_state_mismatch")
        if self.node_kind == "human_gate":
            if self.authority_requirement != "human_gate":
                _fail("human_gate_authority_required")
            if self.state not in {"proposed", "waiting_authority", "completed"}:
                _fail("invalid_human_gate_state")
        if self.node_kind == "rollback" and not self.rollback:
            _fail("rollback_node_steps_required")

    @property
    def node_digest(self) -> str:
        return canonical_digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "objective_id": self.objective_id,
            "title": self.title,
            "purpose": self.purpose,
            "dependencies": list(self.dependencies),
            "node_kind": self.node_kind,
            "capability_requirements": [
                item.to_dict() for item in self.capability_requirements
            ],
            "read_scope": list(self.read_scope),
            "write_scope": list(self.write_scope),
            "risk": self.risk,
            "reversible": self.reversible,
            "authority_requirement": self.authority_requirement,
            "success_criteria": list(self.success_criteria),
            "verification": list(self.verification),
            "rollback": list(self.rollback),
            "timeout_ms": self.timeout_ms,
            "retry_policy": self.retry_policy.to_dict(),
            "state": self.state,
            "evidence": [item.to_dict() for item in self.evidence],
            "omitted_reason": self.omitted_reason,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PlanNode:
        expected = {
            "node_id",
            "objective_id",
            "title",
            "purpose",
            "dependencies",
            "node_kind",
            "capability_requirements",
            "read_scope",
            "write_scope",
            "risk",
            "reversible",
            "authority_requirement",
            "success_criteria",
            "verification",
            "rollback",
            "timeout_ms",
            "retry_policy",
            "state",
            "evidence",
            "omitted_reason",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            _fail("invalid_plan_node")
        payload = dict(value)
        for field in (
            "dependencies",
            "capability_requirements",
            "read_scope",
            "write_scope",
            "success_criteria",
            "verification",
            "rollback",
            "evidence",
        ):
            if not isinstance(payload[field], list):
                _fail("invalid_plan_node")
            payload[field] = tuple(payload[field])
        payload["capability_requirements"] = tuple(
            CapabilityRequirement.from_dict(item)
            for item in payload["capability_requirements"]
        )
        payload["retry_policy"] = RetryPolicy.from_dict(payload["retry_policy"])
        return cls(**payload)


@dataclass(frozen=True)
class PlanEdge:
    edge_id: str
    from_node_id: str
    to_node_id: str
    edge_kind: str = "sequence"
    condition: str | None = None
    priority: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "edge_id", _validate_identity(self.edge_id, "invalid_plan_edge_id")
        )
        object.__setattr__(
            self,
            "from_node_id",
            _validate_identity(self.from_node_id, "invalid_plan_edge_source"),
        )
        object.__setattr__(
            self,
            "to_node_id",
            _validate_identity(self.to_node_id, "invalid_plan_edge_target"),
        )
        if self.from_node_id == self.to_node_id:
            _fail("plan_edge_self_loop")
        object.__setattr__(
            self,
            "edge_kind",
            _validate_enum(self.edge_kind, EDGE_KINDS, "invalid_plan_edge_kind"),
        )
        object.__setattr__(
            self,
            "condition",
            _normalize_optional_text(
                self.condition, reason_code="invalid_plan_edge_condition"
            ),
        )
        _require_int(
            self.priority,
            minimum=0,
            maximum=1_000_000,
            reason_code="invalid_plan_edge_priority",
        )
        if self.edge_kind in {"decision", "failure", "alternative"}:
            if self.condition is None:
                _fail("conditional_plan_edge_requires_condition")
        elif self.condition is not None:
            _fail("unconditional_plan_edge_condition_forbidden")

    @property
    def edge_digest(self) -> str:
        return canonical_digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "from_node_id": self.from_node_id,
            "to_node_id": self.to_node_id,
            "edge_kind": self.edge_kind,
            "condition": self.condition,
            "priority": self.priority,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PlanEdge:
        expected = {
            "edge_id",
            "from_node_id",
            "to_node_id",
            "edge_kind",
            "condition",
            "priority",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            _fail("invalid_plan_edge")
        return cls(**dict(value))


@dataclass(frozen=True)
class PlanRevision:
    revision_id: str
    plan_id: str
    from_version: int | None
    to_version: int
    change_reason: str
    authority_impact: str
    previous_digest: str | None
    plan_digest: str
    revision_digest: str
    created_at: str
    created_by: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "revision_id",
            _validate_identity(self.revision_id, "invalid_plan_revision_id"),
        )
        object.__setattr__(
            self, "plan_id", _validate_identity(self.plan_id, "invalid_plan_id")
        )
        _require_int(
            self.to_version,
            minimum=1,
            maximum=_ABSOLUTE_MAX_REVISIONS,
            reason_code="invalid_plan_revision_to_version",
        )
        if self.from_version is not None:
            _require_int(
                self.from_version,
                minimum=1,
                maximum=_ABSOLUTE_MAX_REVISIONS,
                reason_code="invalid_plan_revision_from_version",
            )
        object.__setattr__(
            self,
            "change_reason",
            _normalize_text(
                self.change_reason, reason_code="invalid_plan_change_reason"
            ),
        )
        object.__setattr__(
            self,
            "authority_impact",
            _validate_enum(
                self.authority_impact,
                AUTHORITY_IMPACTS,
                "invalid_plan_authority_impact",
            ),
        )
        if self.previous_digest is not None:
            _validate_digest(
                self.previous_digest, "invalid_plan_previous_digest"
            )
        _validate_digest(self.plan_digest, "invalid_plan_digest")
        object.__setattr__(
            self,
            "created_at",
            _normalize_timestamp(
                self.created_at, "invalid_plan_revision_created_at"
            ),
        )
        object.__setattr__(
            self,
            "created_by",
            _validate_enum(
                self.created_by, REVISION_ACTORS, "invalid_plan_revision_actor"
            ),
        )
        if self.to_version == 1:
            if self.from_version is not None or self.previous_digest is not None:
                _fail("initial_plan_revision_parent_forbidden")
        elif (
            self.from_version is None
            or self.to_version != self.from_version + 1
            or self.previous_digest is None
        ):
            _fail("plan_revision_parent_required")
        expected = canonical_digest(self.identity_payload())
        if self.revision_digest and self.revision_digest != expected:
            _fail("plan_revision_digest_mismatch")
        object.__setattr__(self, "revision_digest", expected)

    @property
    def revision_number(self) -> int:
        return self.to_version

    def identity_payload(self) -> dict[str, Any]:
        return {
            "revision_id": self.revision_id,
            "plan_id": self.plan_id,
            "from_version": self.from_version,
            "to_version": self.to_version,
            "change_reason": self.change_reason,
            "authority_impact": self.authority_impact,
            "previous_digest": self.previous_digest,
            "plan_digest": self.plan_digest,
            "created_at": self.created_at,
            "created_by": self.created_by,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self.identity_payload()
        payload["revision_digest"] = self.revision_digest
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PlanRevision:
        expected = {
            "revision_id",
            "plan_id",
            "from_version",
            "to_version",
            "change_reason",
            "authority_impact",
            "previous_digest",
            "plan_digest",
            "revision_digest",
            "created_at",
            "created_by",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            _fail("invalid_plan_revision")
        return cls(**dict(value))


def _graph_identity_payload(
    *,
    plan_id: str,
    objective_id: str,
    objective_version: int,
    objective_digest: str,
    nodes: Sequence[PlanNode],
    edges: Sequence[PlanEdge],
    entry_node_ids: Sequence[str],
    terminal_node_ids: Sequence[str],
    version: int,
) -> dict[str, Any]:
    return {
        "plan_id": plan_id,
        "objective_id": objective_id,
        "objective_version": objective_version,
        "objective_digest": objective_digest,
        "nodes": [item.to_dict() for item in nodes],
        "edges": [item.to_dict() for item in edges],
        "entry_node_ids": list(entry_node_ids),
        "terminal_node_ids": list(terminal_node_ids),
        "version": version,
    }


@dataclass(frozen=True)
class PlanGraph:
    plan_id: str
    objective_id: str
    objective_version: int
    objective_digest: str
    nodes: tuple[PlanNode, ...]
    edges: tuple[PlanEdge, ...]
    revisions: tuple[PlanRevision, ...]
    entry_node_ids: tuple[str, ...]
    terminal_node_ids: tuple[str, ...]
    plan_digest: str = ""
    version: int = PLAN_GRAPH_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "plan_id", _validate_identity(self.plan_id, "invalid_plan_id")
        )
        object.__setattr__(
            self,
            "objective_id",
            _validate_identity(self.objective_id, "invalid_plan_objective_id"),
        )
        _require_int(
            self.objective_version,
            minimum=1,
            maximum=1_000_000,
            reason_code="invalid_plan_objective_version",
        )
        _validate_digest(self.objective_digest, "invalid_plan_objective_digest")
        _require_int(
            self.version,
            minimum=1,
            maximum=_ABSOLUTE_MAX_REVISIONS,
            reason_code="invalid_plan_version",
        )
        if isinstance(self.nodes, (str, bytes)) or not isinstance(
            self.nodes, Sequence
        ):
            _fail("invalid_plan_nodes")
        nodes = tuple(sorted((_coerce_node(item) for item in self.nodes), key=lambda item: item.node_id))
        if not nodes:
            _fail("plan_node_required")
        if len({item.node_id for item in nodes}) != len(nodes):
            _fail("duplicate_plan_node")
        object.__setattr__(self, "nodes", nodes)
        if isinstance(self.edges, (str, bytes)) or not isinstance(
            self.edges, Sequence
        ):
            _fail("invalid_plan_edges")
        edges = tuple(
            sorted(
                (_coerce_edge(item) for item in self.edges),
                key=lambda item: (
                    item.from_node_id,
                    item.priority,
                    item.edge_id,
                    item.to_node_id,
                ),
            )
        )
        if len({item.edge_id for item in edges}) != len(edges):
            _fail("duplicate_plan_edge")
        object.__setattr__(self, "edges", edges)
        entries = _normalize_identities(
            self.entry_node_ids, reason_code="invalid_plan_entry_node"
        )
        terminals = _normalize_identities(
            self.terminal_node_ids, reason_code="invalid_plan_terminal_node"
        )
        object.__setattr__(self, "entry_node_ids", entries)
        object.__setattr__(self, "terminal_node_ids", terminals)
        _validate_graph_structure(
            objective_id=self.objective_id,
            nodes=nodes,
            edges=edges,
            entry_node_ids=entries,
            terminal_node_ids=terminals,
        )
        expected = canonical_digest(self.identity_payload())
        if self.plan_digest and self.plan_digest != expected:
            _fail("plan_digest_mismatch")
        object.__setattr__(self, "plan_digest", expected)
        if isinstance(self.revisions, (str, bytes)) or not isinstance(
            self.revisions, Sequence
        ):
            _fail("invalid_plan_revisions")
        revisions = tuple(
            sorted(
                (
                    item
                    if isinstance(item, PlanRevision)
                    else PlanRevision.from_dict(item)
                    for item in self.revisions
                ),
                key=lambda item: item.revision_number,
            )
        )
        if not revisions:
            _fail("plan_revision_required")
        object.__setattr__(self, "revisions", revisions)
        _validate_revision_chain(self, revisions)

    def identity_payload(self) -> dict[str, Any]:
        return _graph_identity_payload(
            plan_id=self.plan_id,
            objective_id=self.objective_id,
            objective_version=self.objective_version,
            objective_digest=self.objective_digest,
            nodes=self.nodes,
            edges=self.edges,
            entry_node_ids=self.entry_node_ids,
            terminal_node_ids=self.terminal_node_ids,
            version=self.version,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "objective_id": self.objective_id,
            "objective_version": self.objective_version,
            "objective_digest": self.objective_digest,
            "nodes": [item.to_dict() for item in self.nodes],
            "edges": [item.to_dict() for item in self.edges],
            "revisions": [item.to_dict() for item in self.revisions],
            "entry_node_ids": list(self.entry_node_ids),
            "terminal_node_ids": list(self.terminal_node_ids),
            "plan_digest": self.plan_digest,
            "version": self.version,
        }

    def to_envelope(self) -> dict[str, Any]:
        return {
            "schema_generation": COGNITIVE_SCHEMA_GENERATION,
            "document_type": "plan_graph",
            "document": self.to_dict(),
        }

    def to_canonical_json(self) -> str:
        return canonical_json(self.to_envelope())

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PlanGraph:
        expected = {
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
        if not isinstance(value, Mapping) or set(value) != expected:
            _fail("invalid_plan_graph")
        payload = dict(value)
        for field in (
            "nodes",
            "edges",
            "revisions",
            "entry_node_ids",
            "terminal_node_ids",
        ):
            if not isinstance(payload[field], list):
                _fail("invalid_plan_graph")
        payload["nodes"] = tuple(PlanNode.from_dict(item) for item in payload["nodes"])
        payload["edges"] = tuple(PlanEdge.from_dict(item) for item in payload["edges"])
        payload["revisions"] = tuple(
            PlanRevision.from_dict(item) for item in payload["revisions"]
        )
        payload["entry_node_ids"] = tuple(payload["entry_node_ids"])
        payload["terminal_node_ids"] = tuple(payload["terminal_node_ids"])
        graph = cls(**payload)
        _enforce_budget(graph, DEFAULT_PLANNING_BUDGET)
        return graph


def _validate_graph_structure(
    *,
    objective_id: str,
    nodes: Sequence[PlanNode],
    edges: Sequence[PlanEdge],
    entry_node_ids: Sequence[str],
    terminal_node_ids: Sequence[str],
) -> None:
    node_by_id = {item.node_id: item for item in nodes}
    node_ids = set(node_by_id)
    if any(item.objective_id != objective_id for item in nodes):
        _fail("plan_node_objective_mismatch")
    incoming: dict[str, set[str]] = {item: set() for item in node_ids}
    outgoing: dict[str, set[str]] = {item: set() for item in node_ids}
    outgoing_edges: dict[str, list[PlanEdge]] = {item: [] for item in node_ids}
    pair_kinds: set[tuple[str, str, str]] = set()
    for edge in edges:
        if edge.from_node_id not in node_ids or edge.to_node_id not in node_ids:
            _fail("plan_edge_node_missing")
        key = (edge.from_node_id, edge.to_node_id, edge.edge_kind)
        if key in pair_kinds:
            _fail("duplicate_plan_edge_route")
        pair_kinds.add(key)
        incoming[edge.to_node_id].add(edge.from_node_id)
        outgoing[edge.from_node_id].add(edge.to_node_id)
        outgoing_edges[edge.from_node_id].append(edge)
        if edge.from_node_id not in node_by_id[edge.to_node_id].dependencies:
            _fail("plan_edge_dependency_mismatch")
        if edge.edge_kind == "decision" and node_by_id[edge.from_node_id].node_kind != "decision":
            _fail("decision_edge_source_mismatch")
        if edge.edge_kind == "rollback" and node_by_id[edge.to_node_id].node_kind != "rollback":
            _fail("rollback_edge_target_mismatch")
        if edge.edge_kind == "alternative" and node_by_id[edge.to_node_id].node_kind != "alternative":
            _fail("alternative_edge_target_mismatch")
        if edge.edge_kind == "resume" and node_by_id[edge.from_node_id].node_kind != "human_gate":
            _fail("resume_edge_source_mismatch")
    for node in nodes:
        if set(node.dependencies) != incoming[node.node_id]:
            _fail("plan_dependency_edge_mismatch")
    computed_entries = tuple(sorted(item for item in node_ids if not incoming[item]))
    computed_terminals = tuple(sorted(item for item in node_ids if not outgoing[item]))
    for source, source_edges in outgoing_edges.items():
        parallel = [item for item in source_edges if item.edge_kind == "parallel"]
        if parallel and len(parallel) < 2:
            _fail("parallel_plan_requires_multiple_branches")
        decisions = [item for item in source_edges if item.edge_kind == "decision"]
        if node_by_id[source].node_kind == "decision" and len(decisions) < 2:
            _fail("decision_plan_requires_multiple_branches")
        priorities = [(item.edge_kind, item.priority) for item in source_edges]
        if len(priorities) != len(set(priorities)):
            _fail("ambiguous_plan_edge_priority")
    indegree = {node_id: len(incoming[node_id]) for node_id in node_ids}
    ready = sorted(node_id for node_id, count in indegree.items() if count == 0)
    visited: list[str] = []
    while ready:
        current = ready.pop(0)
        visited.append(current)
        for target in sorted(outgoing[current]):
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
                ready.sort()
    if len(visited) != len(node_ids):
        _fail("plan_cycle_detected")
    if not computed_entries or tuple(entry_node_ids) != computed_entries:
        _fail("plan_entry_nodes_mismatch")
    if not computed_terminals or tuple(terminal_node_ids) != computed_terminals:
        _fail("plan_terminal_nodes_mismatch")


def _validate_revision_chain(
    graph: PlanGraph, revisions: Sequence[PlanRevision]
) -> None:
    if len(revisions) != graph.version:
        _fail("plan_revision_version_mismatch")
    for index, revision in enumerate(revisions, start=1):
        if revision.plan_id != graph.plan_id or revision.revision_number != index:
            _fail("plan_revision_chain_invalid")
        if (
            index == 1
            and (
                revision.from_version is not None
                or revision.to_version != 1
                or revision.previous_digest is not None
            )
        ):
            _fail("plan_revision_chain_invalid")
        if index > 1:
            if (
                revision.from_version != index - 1
                or revision.to_version != index
                or revision.previous_digest
                != revisions[index - 2].plan_digest
            ):
                _fail("plan_revision_parent_mismatch")
    if revisions[-1].plan_digest != graph.plan_digest:
        _fail("plan_revision_digest_mismatch")


def _enforce_budget(graph: PlanGraph, budget: PlanningBudget) -> None:
    if not isinstance(budget, PlanningBudget):
        _fail("invalid_planning_budget")
    if len(graph.nodes) > budget.maximum_nodes:
        _fail("plan_node_budget_exceeded")
    if len(graph.edges) > budget.maximum_edges:
        _fail("plan_edge_budget_exceeded")
    if len(graph.revisions) > budget.maximum_revisions:
        _fail("plan_revision_budget_exceeded")
    if sum(len(node.evidence) for node in graph.nodes) > budget.maximum_evidence_items:
        _fail("plan_evidence_budget_exceeded")
    if sum(node.timeout_ms for node in graph.nodes) > budget.maximum_total_timeout_ms:
        _fail("plan_timeout_budget_exceeded")
    if len(graph.to_canonical_json().encode("utf-8")) > budget.maximum_canonical_bytes:
        _fail("plan_canonical_byte_budget_exceeded")


def _derive_boundaries(
    nodes: Sequence[PlanNode], edges: Sequence[PlanEdge]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    node_ids = {item.node_id for item in nodes}
    incoming = {item.to_node_id for item in edges}
    outgoing = {item.from_node_id for item in edges}
    return (
        tuple(sorted(node_ids - incoming)),
        tuple(sorted(node_ids - outgoing)),
    )


def _coerce_objective(
    objective: ObjectiveContract | Mapping[str, Any],
) -> ObjectiveContract:
    if isinstance(objective, ObjectiveContract):
        return objective
    try:
        return ObjectiveContract.from_dict(objective)
    except (OperationalStateError, TypeError) as exc:
        reason = getattr(exc, "reason_code", "invalid_plan_objective")
        raise PlanningError(reason) from None


def _check_objective_bounds(
    objective: ObjectiveContract, nodes: Sequence[PlanNode]
) -> None:
    authority = objective.authority_request
    for node in nodes:
        if node.objective_id != objective.objective_id:
            _fail("plan_node_objective_mismatch")
        for scope in node.read_scope:
            if not _scope_is_within(scope, authority.read_roots):
                _fail("plan_read_scope_expansion_denied")
        prospective_write_roots = authority.write_roots
        if (
            objective.agency_level == "plan"
            and not prospective_write_roots
        ):
            prospective_write_roots = authority.read_roots
        for scope in node.write_scope:
            if not _scope_is_within(scope, prospective_write_roots):
                _fail("plan_write_scope_expansion_denied")
        requested_capabilities = {
            requirement.capability_id
            for requirement in node.capability_requirements
        }
        if (
            requested_capabilities
            and objective.agency_level != "plan"
            and not requested_capabilities.issubset(authority.capabilities)
        ):
            _fail("plan_capability_expansion_denied")
        plan_risk_ceiling = (
            objective.risk_tolerance
            if objective.agency_level == "plan"
            else authority.risk_ceiling
        )
        if _RISK_RANK[node.risk] > _RISK_RANK[plan_risk_ceiling]:
            _fail("plan_risk_expansion_denied")
        if objective.agency_level == "consult" and (
            node.write_scope or requested_capabilities
        ):
            _fail("consult_plan_scope_denied")
        if (
            objective.agency_level == "plan"
            and node.write_scope
            and node.authority_requirement
            not in {"explicit_act", "human_gate"}
        ):
            _fail("planned_write_requires_explicit_authority")


def build_plan_graph(
    *,
    objective: ObjectiveContract | Mapping[str, Any],
    nodes: Sequence[PlanNode | Mapping[str, Any]],
    edges: Sequence[PlanEdge | Mapping[str, Any]] = (),
    change_reason: str,
    created_at: str,
    created_by: str = "agent",
    plan_id: str | None = None,
    budget: PlanningBudget = DEFAULT_PLANNING_BUDGET,
) -> PlanGraph:
    """Build a pure immutable graph. It cannot execute or persist any node."""

    contract = _coerce_objective(objective)
    normalized_nodes = tuple(_coerce_node(item) for item in nodes)
    normalized_edges = tuple(_coerce_edge(item) for item in edges)
    validate_initial_plan_states(normalized_nodes)
    _check_objective_bounds(contract, normalized_nodes)
    entries, terminals = _derive_boundaries(normalized_nodes, normalized_edges)
    seed = {
        "objective_id": contract.objective_id,
        "objective_digest": contract.canonical_digest,
        "nodes": [item.to_dict() for item in sorted(normalized_nodes, key=lambda x: x.node_id)],
        "edges": [item.to_dict() for item in sorted(normalized_edges, key=lambda x: x.edge_id)],
    }
    selected_id = plan_id or make_identity("plan", seed)
    selected_id = _validate_identity(selected_id, "invalid_plan_id")
    ordered_nodes = tuple(sorted(normalized_nodes, key=lambda item: item.node_id))
    ordered_edges = tuple(
        sorted(
            normalized_edges,
            key=lambda item: (
                item.from_node_id,
                item.priority,
                item.edge_id,
                item.to_node_id,
            ),
        )
    )
    digest = canonical_digest(
        _graph_identity_payload(
            plan_id=selected_id,
            objective_id=contract.objective_id,
            objective_version=contract.version,
            objective_digest=contract.canonical_digest,
            nodes=ordered_nodes,
            edges=ordered_edges,
            entry_node_ids=entries,
            terminal_node_ids=terminals,
            version=1,
        )
    )
    revision_seed = {
        "plan_id": selected_id,
        "from_version": None,
        "to_version": 1,
        "change_reason": change_reason,
        "authority_impact": "none",
        "previous_digest": None,
        "plan_digest": digest,
        "created_at": _normalize_timestamp(
            created_at, "invalid_plan_revision_created_at"
        ),
        "created_by": created_by,
    }
    revision = PlanRevision(
        revision_id=make_identity("revision", revision_seed),
        revision_digest="",
        **revision_seed,
    )
    graph = PlanGraph(
        plan_id=selected_id,
        objective_id=contract.objective_id,
        objective_version=contract.version,
        objective_digest=contract.canonical_digest,
        nodes=ordered_nodes,
        edges=ordered_edges,
        revisions=(revision,),
        entry_node_ids=entries,
        terminal_node_ids=terminals,
        plan_digest=digest,
        version=1,
    )
    _enforce_budget(graph, budget)
    return graph


@dataclass(frozen=True)
class PlanComparison:
    same_objective: bool
    added_nodes: tuple[str, ...]
    removed_nodes: tuple[str, ...]
    changed_nodes: tuple[str, ...]
    added_edges: tuple[str, ...]
    removed_edges: tuple[str, ...]
    changed_edges: tuple[str, ...]
    added_capabilities: tuple[str, ...]
    removed_capabilities: tuple[str, ...]
    added_read_scopes: tuple[str, ...]
    removed_read_scopes: tuple[str, ...]
    added_write_scopes: tuple[str, ...]
    removed_write_scopes: tuple[str, ...]
    risk_increased: bool
    scope_expanded: bool
    authority_impact: str

    @property
    def materially_changed(self) -> bool:
        return bool(
            self.added_nodes
            or self.removed_nodes
            or self.changed_nodes
            or self.added_edges
            or self.removed_edges
            or self.changed_edges
            or self.added_capabilities
            or self.removed_capabilities
            or self.added_read_scopes
            or self.removed_read_scopes
            or self.added_write_scopes
            or self.removed_write_scopes
            or self.risk_increased
        )


def _collect(values: Sequence[PlanNode], field: str) -> set[str]:
    result: set[str] = set()
    for value in values:
        if field == "capability_requirements":
            result.update(
                f"{item.capability_id}@{item.capability_version}:"
                f"{item.capability_digest}"
                for item in value.capability_requirements
            )
        else:
            result.update(getattr(value, field))
    return result


def compare_plans(
    previous: PlanGraph, candidate: PlanGraph
) -> PlanComparison:
    if not isinstance(previous, PlanGraph) or not isinstance(candidate, PlanGraph):
        _fail("invalid_plan_comparison")
    old_nodes = {item.node_id: item for item in previous.nodes}
    new_nodes = {item.node_id: item for item in candidate.nodes}
    added = set(new_nodes) - set(old_nodes)
    removed = set(old_nodes) - set(new_nodes)
    changed = {
        node_id
        for node_id in set(old_nodes) & set(new_nodes)
        if old_nodes[node_id].to_dict() != new_nodes[node_id].to_dict()
    }
    old_edges = {item.edge_id: item for item in previous.edges}
    new_edges = {item.edge_id: item for item in candidate.edges}
    added_edges = set(new_edges) - set(old_edges)
    removed_edges = set(old_edges) - set(new_edges)
    changed_edges = {
        edge_id
        for edge_id in set(old_edges) & set(new_edges)
        if old_edges[edge_id].to_dict() != new_edges[edge_id].to_dict()
    }
    fields = (
        "capability_requirements",
        "read_scope",
        "write_scope",
    )
    old_sets = {field: _collect(previous.nodes, field) for field in fields}
    new_sets = {field: _collect(candidate.nodes, field) for field in fields}
    old_risk = max(_RISK_RANK[item.risk] for item in previous.nodes)
    new_risk = max(_RISK_RANK[item.risk] for item in candidate.nodes)
    scope_expanded = any(
        new_sets[field] - old_sets[field]
        for field in ("read_scope", "write_scope", "capability_requirements")
    )
    risk_increased = new_risk > old_risk
    has_addition = bool(added or scope_expanded or risk_increased)
    has_removal = bool(
        removed
        or any(old_sets[field] - new_sets[field] for field in fields)
    )
    impact = (
        "requires_new_authority"
        if has_addition
        else "narrows_scope"
        if has_removal
        else "none"
    )
    return PlanComparison(
        same_objective=(
            previous.objective_id == candidate.objective_id
            and previous.objective_version == candidate.objective_version
            and previous.objective_digest == candidate.objective_digest
            and previous.plan_id == candidate.plan_id
        ),
        added_nodes=tuple(sorted(added)),
        removed_nodes=tuple(sorted(removed)),
        changed_nodes=tuple(sorted(changed)),
        added_edges=tuple(sorted(added_edges)),
        removed_edges=tuple(sorted(removed_edges)),
        changed_edges=tuple(sorted(changed_edges)),
        added_capabilities=tuple(
            sorted(new_sets["capability_requirements"] - old_sets["capability_requirements"])
        ),
        removed_capabilities=tuple(
            sorted(old_sets["capability_requirements"] - new_sets["capability_requirements"])
        ),
        added_read_scopes=tuple(sorted(new_sets["read_scope"] - old_sets["read_scope"])),
        removed_read_scopes=tuple(sorted(old_sets["read_scope"] - new_sets["read_scope"])),
        added_write_scopes=tuple(
            sorted(new_sets["write_scope"] - old_sets["write_scope"])
        ),
        removed_write_scopes=tuple(
            sorted(old_sets["write_scope"] - new_sets["write_scope"])
        ),
        risk_increased=risk_increased,
        scope_expanded=scope_expanded,
        authority_impact=impact,
    )


def validate_initial_plan_states(nodes: Sequence[PlanNode]) -> None:
    """Reject pre-completed or runtime-active nodes at every persistence boundary."""

    for item in nodes:
        node = _coerce_node(item)
        if node.state in {"proposed", "omitted"}:
            continue
        if node.node_kind == "human_gate" and node.state == "waiting_authority":
            continue
        _fail("initial_plan_node_state_forbidden")


def validate_plan_state_transition(
    previous: PlanGraph,
    candidate: PlanGraph,
    *,
    mode: str = "revision",
) -> str:
    """Validate the closed set of planner-owned state transitions.

    Execution and terminal transitions belong to the supervised runtime, not
    Plan revision.  The only v10-B control transitions are explicit pause and
    resume operations.
    """

    if not isinstance(previous, PlanGraph) or not isinstance(candidate, PlanGraph):
        _fail("invalid_plan_state_transition")
    if mode not in {"revision", "pause", "resume", "persistence"}:
        _fail("invalid_plan_state_transition_mode")
    old = {item.node_id: item for item in previous.nodes}
    new = {item.node_id: item for item in candidate.nodes}
    validate_initial_plan_states(
        tuple(item for node_id, item in new.items() if node_id not in old)
    )
    changes = [
        (old[node_id], new[node_id])
        for node_id in sorted(set(old) & set(new))
        if old[node_id].state != new[node_id].state
    ]
    if not changes:
        return "none"

    pause_allowed = all(
        before.state in {"proposed", "ready", "running", "verifying"}
        and after.state == "waiting_external"
        for before, after in changes
    )
    resume_allowed = all(
        (
            before.node_kind == "human_gate"
            and before.state == "waiting_authority"
            and after.state == "completed"
        )
        or (
            before.node_kind != "human_gate"
            and before.state in {"waiting_external", "waiting_authority"}
            and after.state == "ready"
        )
        for before, after in changes
    )
    completes_human_gate = any(
        before.node_kind == "human_gate"
        and before.state == "waiting_authority"
        and after.state == "completed"
        for before, after in changes
    )
    if (
        completes_human_gate
        and candidate.revisions[-1].created_by != "user"
    ):
        _fail("human_gate_resume_requires_user")
    if mode == "pause" and pause_allowed:
        return "pause"
    if mode == "resume" and resume_allowed:
        return "resume"
    if mode == "persistence":
        if pause_allowed:
            return "pause"
        if resume_allowed:
            return "resume"
    _fail("plan_runtime_state_transition_denied")


def capability_node_constraint_reasons(
    node: PlanNode,
    profile: CapabilityProfile,
    *,
    include_availability: bool = True,
) -> tuple[str, ...]:
    """Return monotonic profile-to-node safety violations."""

    if not isinstance(node, PlanNode) or not isinstance(profile, CapabilityProfile):
        _fail("invalid_plan_capability_binding")
    reasons: set[str] = set()
    if include_availability and profile.availability != "available":
        reasons.add(f"capability_{profile.availability}")
    if _RISK_RANK[node.risk] < _RISK_RANK[profile.risk_class]:
        reasons.add("plan_node_capability_risk_understated")
    if (
        _AUTHORITY_RANK[node.authority_requirement]
        < _AUTHORITY_RANK[profile.required_authority]
    ):
        reasons.add("plan_node_capability_authority_understated")
    if not profile.reversible and node.reversible:
        reasons.add("plan_node_capability_reversibility_overstated")
    if profile.read_scopes and not node.read_scope:
        reasons.add("plan_node_capability_read_scope_missing")
    if profile.write_scopes:
        if not node.write_scope:
            reasons.add("plan_node_capability_write_scope_missing")
        if node.authority_requirement not in {"explicit_act", "human_gate"}:
            reasons.add("plan_node_write_capability_requires_explicit_authority")
    return tuple(sorted(reasons))


def _revise_plan(
    previous: PlanGraph,
    *,
    objective: ObjectiveContract | Mapping[str, Any],
    nodes: Sequence[PlanNode | Mapping[str, Any]] | None = None,
    edges: Sequence[PlanEdge | Mapping[str, Any]] | None = None,
    change_reason: str,
    created_at: str,
    created_by: str = "agent",
    budget: PlanningBudget = DEFAULT_PLANNING_BUDGET,
    transition_mode: str,
) -> PlanGraph:
    if not isinstance(previous, PlanGraph):
        _fail("invalid_parent_plan")
    contract = _coerce_objective(objective)
    if (
        contract.objective_id != previous.objective_id
        or contract.version != previous.objective_version
        or contract.canonical_digest != previous.objective_digest
    ):
        _fail("plan_root_objective_change_denied")
    normalized_nodes = tuple(
        _coerce_node(item) for item in (nodes if nodes is not None else previous.nodes)
    )
    normalized_edges = tuple(
        _coerce_edge(item) for item in (edges if edges is not None else previous.edges)
    )
    _check_objective_bounds(contract, normalized_nodes)
    entries, terminals = _derive_boundaries(normalized_nodes, normalized_edges)
    version = previous.version + 1
    ordered_nodes = tuple(sorted(normalized_nodes, key=lambda item: item.node_id))
    ordered_edges = tuple(
        sorted(
            normalized_edges,
            key=lambda item: (
                item.from_node_id,
                item.priority,
                item.edge_id,
                item.to_node_id,
            ),
        )
    )
    digest = canonical_digest(
        _graph_identity_payload(
            plan_id=previous.plan_id,
            objective_id=previous.objective_id,
            objective_version=previous.objective_version,
            objective_digest=previous.objective_digest,
            nodes=ordered_nodes,
            edges=ordered_edges,
            entry_node_ids=entries,
            terminal_node_ids=terminals,
            version=version,
        )
    )
    provisional_seed = {
        "plan_id": previous.plan_id,
        "from_version": previous.version,
        "to_version": version,
        "change_reason": change_reason,
        "authority_impact": "none",
        "previous_digest": previous.plan_digest,
        "plan_digest": digest,
        "created_at": _normalize_timestamp(
            created_at, "invalid_plan_revision_created_at"
        ),
        "created_by": created_by,
    }
    provisional_revision = PlanRevision(
        revision_id=make_identity("revision", provisional_seed),
        revision_digest="",
        **provisional_seed,
    )
    provisional = PlanGraph(
        plan_id=previous.plan_id,
        objective_id=previous.objective_id,
        objective_version=previous.objective_version,
        objective_digest=previous.objective_digest,
        nodes=ordered_nodes,
        edges=ordered_edges,
        revisions=previous.revisions + (provisional_revision,),
        entry_node_ids=entries,
        terminal_node_ids=terminals,
        plan_digest=digest,
        version=version,
    )
    validate_plan_state_transition(
        previous,
        provisional,
        mode=transition_mode,
    )
    comparison = compare_plans(previous, provisional)
    if not comparison.materially_changed:
        _fail("plan_revision_no_progress")
    if comparison.authority_impact == "requires_new_authority":
        _fail("plan_revision_authority_expansion_denied")
    revision_seed = dict(provisional_seed)
    revision_seed["authority_impact"] = comparison.authority_impact
    revision = PlanRevision(
        revision_id=make_identity("revision", revision_seed),
        revision_digest="",
        **revision_seed,
    )
    graph = replace(
        provisional,
        revisions=previous.revisions + (revision,),
    )
    _enforce_budget(graph, budget)
    return graph


def revise_plan(
    previous: PlanGraph,
    *,
    objective: ObjectiveContract | Mapping[str, Any],
    nodes: Sequence[PlanNode | Mapping[str, Any]] | None = None,
    edges: Sequence[PlanEdge | Mapping[str, Any]] | None = None,
    change_reason: str,
    created_at: str,
    created_by: str = "agent",
    budget: PlanningBudget = DEFAULT_PLANNING_BUDGET,
) -> PlanGraph:
    return _revise_plan(
        previous,
        objective=objective,
        nodes=nodes,
        edges=edges,
        change_reason=change_reason,
        created_at=created_at,
        created_by=created_by,
        budget=budget,
        transition_mode="revision",
    )


@dataclass(frozen=True)
class PlanFeasibility:
    feasible: bool
    reason_codes: tuple[str, ...]
    waiting_node_ids: tuple[str, ...]
    maximum_risk: str
    requires_human_gate: bool


def assess_plan_feasibility(
    graph: PlanGraph,
    objective: ObjectiveContract | Mapping[str, Any],
    registry: CapabilityRegistry | None = None,
) -> PlanFeasibility:
    if not isinstance(graph, PlanGraph):
        _fail("invalid_plan_graph")
    contract = _coerce_objective(objective)
    reasons: set[str] = set()
    waiting: set[str] = set()
    if (
        graph.objective_id != contract.objective_id
        or graph.objective_version != contract.version
        or graph.objective_digest != contract.canonical_digest
    ):
        reasons.add("plan_objective_binding_mismatch")
    if contract.clarification_required:
        reasons.add("objective_clarification_required")
    if registry is None:
        registry = build_suggested_capability_registry()
    if not isinstance(registry, CapabilityRegistry):
        _fail("invalid_capability_registry")
    for node in graph.nodes:
        required_agency = _AUTHORITY_AGENCY[node.authority_requirement]
        if _AGENCY_RANK[required_agency] > _AGENCY_RANK[contract.agency_level]:
            reasons.add("plan_node_authority_unavailable")
            waiting.add(node.node_id)
        if node.state in {"waiting_authority", "waiting_external"}:
            waiting.add(node.node_id)
        if not node.reversible:
            reasons.add("irreversible_plan_node")
            waiting.add(node.node_id)
        if node.write_scope and contract.agency_level in {"consult", "plan"}:
            reasons.add("plan_execution_forbidden_at_agency_level")
            waiting.add(node.node_id)
        for requirement in node.capability_requirements:
            try:
                profile = registry.lookup(
                    requirement.capability_id,
                    version=requirement.capability_version,
                    expected_digest=requirement.capability_digest,
                )
            except CognitiveCapabilityError as exc:
                reasons.add(f"plan_{exc.reason_code}")
                waiting.add(node.node_id)
                continue
            binding_reasons = capability_node_constraint_reasons(node, profile)
            if binding_reasons:
                reasons.update(binding_reasons)
                waiting.add(node.node_id)
    return PlanFeasibility(
        feasible=not reasons and not waiting,
        reason_codes=tuple(sorted(reasons)),
        waiting_node_ids=tuple(sorted(waiting)),
        maximum_risk=max(graph.nodes, key=lambda item: _RISK_RANK[item.risk]).risk,
        requires_human_gate=any(
            item.node_kind == "human_gate"
            or item.authority_requirement == "human_gate"
            for item in graph.nodes
        ),
    )


def pause_plan(
    graph: PlanGraph,
    *,
    objective: ObjectiveContract | Mapping[str, Any],
    node_ids: Sequence[str],
    reason: str,
    created_at: str,
    created_by: str = "user",
) -> PlanGraph:
    selected = set(
        _normalize_identities(node_ids, reason_code="invalid_pause_node_id")
    )
    if not selected:
        _fail("pause_node_required")
    available = {item.node_id for item in graph.nodes}
    if not selected.issubset(available):
        _fail("pause_node_missing")
    nodes = []
    for node in graph.nodes:
        if node.node_id not in selected:
            nodes.append(node)
            continue
        if node.state not in {"proposed", "ready", "running", "verifying"}:
            _fail("invalid_plan_pause_transition")
        nodes.append(replace(node, state="waiting_external"))
    return _revise_plan(
        graph,
        objective=objective,
        nodes=nodes,
        change_reason=reason,
        created_at=created_at,
        created_by=created_by,
        transition_mode="pause",
    )


def resume_plan(
    graph: PlanGraph,
    *,
    objective: ObjectiveContract | Mapping[str, Any],
    node_ids: Sequence[str],
    reason: str,
    created_at: str,
    created_by: str = "user",
) -> PlanGraph:
    selected = set(
        _normalize_identities(node_ids, reason_code="invalid_resume_node_id")
    )
    if not selected:
        _fail("resume_node_required")
    available = {item.node_id for item in graph.nodes}
    if not selected.issubset(available):
        _fail("resume_node_missing")
    if created_by != "user" and any(
        item.node_id in selected and item.node_kind == "human_gate"
        for item in graph.nodes
    ):
        _fail("human_gate_resume_requires_user")
    nodes = []
    for node in graph.nodes:
        if node.node_id not in selected:
            nodes.append(node)
            continue
        if node.state not in {"waiting_external", "waiting_authority"}:
            _fail("invalid_plan_resume_transition")
        nodes.append(
            replace(
                node,
                state=(
                    "completed"
                    if node.node_kind == "human_gate"
                    else "ready"
                ),
            )
        )
    return _revise_plan(
        graph,
        objective=objective,
        nodes=nodes,
        change_reason=reason,
        created_at=created_at,
        created_by=created_by,
        transition_mode="resume",
    )


@dataclass(frozen=True)
class WorkflowCandidate:
    candidate_id: str
    source_plan_id: str
    source_plan_digest: str
    title: str
    node_kinds: tuple[str, ...]
    candidate_only: bool = True
    writes_long_term_memory: bool = False

    def __post_init__(self) -> None:
        _validate_identity(self.candidate_id, "invalid_workflow_candidate_id")
        _validate_identity(self.source_plan_id, "invalid_plan_id")
        _validate_digest(
            self.source_plan_digest, "invalid_workflow_source_digest"
        )
        object.__setattr__(
            self,
            "title",
            _normalize_text(
                self.title, reason_code="invalid_workflow_candidate_title"
            ),
        )
        object.__setattr__(
            self,
            "node_kinds",
            tuple(
                sorted(
                    {
                        _validate_enum(
                            item,
                            NODE_KINDS,
                            "invalid_workflow_candidate_node_kind",
                        )
                        for item in self.node_kinds
                    }
                )
            ),
        )
        if not self.candidate_only or self.writes_long_term_memory:
            _fail("workflow_candidate_persistence_denied")

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "source_plan_id": self.source_plan_id,
            "source_plan_digest": self.source_plan_digest,
            "title": self.title,
            "node_kinds": list(self.node_kinds),
            "candidate_only": True,
            "writes_long_term_memory": False,
        }


def propose_workflow_candidate(
    graph: PlanGraph, *, title: str
) -> WorkflowCandidate:
    """Return an in-memory candidate only; no persistence path is provided."""

    if not isinstance(graph, PlanGraph):
        _fail("invalid_plan_graph")
    seed = {
        "source_plan_id": graph.plan_id,
        "source_plan_digest": graph.plan_digest,
        "title": _normalize_text(
            title, reason_code="invalid_workflow_candidate_title"
        ),
        "node_kinds": sorted({item.node_kind for item in graph.nodes}),
    }
    return WorkflowCandidate(
        candidate_id=make_identity("workflow", seed),
        source_plan_id=graph.plan_id,
        source_plan_digest=graph.plan_digest,
        title=seed["title"],
        node_kinds=tuple(seed["node_kinds"]),
    )
