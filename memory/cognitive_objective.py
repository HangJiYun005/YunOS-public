from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping, Sequence

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


COGNITIVE_SCHEMA_GENERATION = 10001
OBJECTIVE_CONTRACT_VERSION = 1

AGENCY_LEVELS = ("consult", "plan", "prepare", "act")
COGNITIVE_STANCES = ("continue", "explore", "breakthrough")
RISK_CLASSES = ("low", "medium", "high", "critical")
CONTEXT_SOURCE_SYSTEMS = (
    "v7_memory",
    "v9_now",
    "v9_resume",
    "project",
    "runtime",
)
HARD_CLARIFICATION_KINDS = frozenset(
    {
        "material_goal_change",
        "material_risk_change",
        "irreversible_operation",
        "new_write_scope",
        "account_action",
        "money_action",
        "external_publication",
        "major_value_conflict",
        "evidence_conflict",
        "missing_success_criteria",
    }
)
LOW_IMPACT_KINDS = frozenset(
    {
        "required_project_read",
        "existing_test_run",
        "git_status_check",
        "obvious_local_fix",
        "verification_report",
        "authorized_reversible_step",
    }
)

DEFAULT_PROHIBITED_OUTCOMES = (
    "create_unrequested_next_version",
    "execute_without_authorization_artifact",
    "self_elevate_agency_level",
    "treat_agent_report_as_user_acceptance",
    "write_long_term_profile_from_single_outcome",
)

_AGENCY_RANK = {level: rank for rank, level in enumerate(AGENCY_LEVELS)}
_MAX_CONTEXT_ITEMS = 64
_MAX_CONTEXT_BYTES = 131_072
_MAX_STRING_ITEMS = 256
_MAX_CONTRACT_BYTES = 262_144
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
    re.compile(
        r"(?i)\bauthorization\s*:\s*(?:bearer|basic)\s+"
        r"[a-z0-9._~+/=-]{8,}"
    ),
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"\beyJ[a-zA-Z0-9_-]{8,}\.[a-zA-Z0-9_-]{8,}\.[a-zA-Z0-9_-]{8,}\b"),
)


class ObjectiveIntentError(OperationalStateError):
    """Fail-closed validation error for the pure Objective/Intent contract."""


def _fail(reason_code: str) -> None:
    raise ObjectiveIntentError(reason_code)


def _reject_secret_literal(value: Any) -> None:
    if isinstance(value, str) and any(
        pattern.search(value) for pattern in _SECRET_LITERAL_PATTERNS
    ):
        _fail("objective_secret_literal_forbidden")


def _require_bool(value: Any, reason_code: str) -> bool:
    if not isinstance(value, bool):
        _fail(reason_code)
    return value


def _require_int(
    value: Any,
    *,
    minimum: int,
    maximum: int | None = None,
    reason_code: str,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        _fail(reason_code)
    if maximum is not None and value > maximum:
        _fail(reason_code)
    return value


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
    _reject_secret_literal(normalized)
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
    values: Sequence[str] | Iterable[str],
    *,
    reason_code: str,
    maximum: int = 4096,
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        _fail(reason_code)
    materialized = list(values)
    if len(materialized) > _MAX_STRING_ITEMS:
        _fail("objective_collection_limit_exceeded")
    normalized = {
        _normalize_text(item, reason_code=reason_code, maximum=maximum)
        for item in materialized
    }
    return tuple(sorted(normalized))


def _normalize_identities(
    values: Sequence[str] | Iterable[str],
    *,
    reason_code: str,
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        _fail(reason_code)
    materialized = list(values)
    if len(materialized) > _MAX_STRING_ITEMS:
        _fail("objective_collection_limit_exceeded")
    normalized: set[str] = set()
    for item in materialized:
        _reject_secret_literal(item)
        try:
            normalized.add(validate_identity(item, reason_code))
        except OperationalStateError as exc:
            raise ObjectiveIntentError(exc.reason_code) from None
    return tuple(sorted(normalized))


def _validate_enum(value: Any, allowed: Sequence[str], reason_code: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        _fail(reason_code)
    return value


ObjectiveSourceAnchor = SourceAnchor


def _validate_objective_source_anchor(
    anchor: ObjectiveSourceAnchor,
) -> ObjectiveSourceAnchor:
    if not isinstance(anchor, ObjectiveSourceAnchor):
        _fail("invalid_objective_source_anchor")
    _normalize_text(
        anchor.locator,
        reason_code="invalid_objective_anchor_locator",
        maximum=512,
    )
    return anchor


@dataclass(frozen=True)
class AgencySelection:
    selected_level: str
    selected_by: str
    maximum_authorized_level: str
    source_anchor_id: str | None
    source_anchor_digest: str | None
    selection_digest: str = ""

    def __post_init__(self) -> None:
        selected_level = _validate_enum(
            self.selected_level,
            AGENCY_LEVELS,
            "invalid_agency_level",
        )
        maximum = _validate_enum(
            self.maximum_authorized_level,
            AGENCY_LEVELS,
            "invalid_maximum_agency_level",
        )
        if self.selected_by not in {"default", "user", "preauthorized_rule"}:
            _fail("untrusted_agency_selector")
        if _AGENCY_RANK[selected_level] > _AGENCY_RANK[maximum]:
            _fail("agency_level_exceeds_authorized_maximum")
        if self.selected_by == "default":
            if selected_level != "consult" or maximum != "consult":
                _fail("default_agency_must_be_consult")
            if self.source_anchor_id is not None or self.source_anchor_digest is not None:
                _fail("default_agency_cannot_claim_authority_source")
        else:
            if self.source_anchor_id is None or self.source_anchor_digest is None:
                _fail("agency_selection_source_required")
            _reject_secret_literal(self.source_anchor_id)
            try:
                validate_identity(
                    self.source_anchor_id,
                    "invalid_agency_selection_source_anchor",
                )
                validate_sha256(
                    self.source_anchor_digest,
                    "invalid_agency_selection_source_digest",
                )
            except OperationalStateError as exc:
                raise ObjectiveIntentError(exc.reason_code) from None
        expected = canonical_digest(self.identity_payload())
        if self.selection_digest and self.selection_digest != expected:
            _fail("agency_selection_digest_mismatch")
        object.__setattr__(self, "selection_digest", expected)

    def identity_payload(self) -> dict[str, Any]:
        return {
            "selected_level": self.selected_level,
            "selected_by": self.selected_by,
            "maximum_authorized_level": self.maximum_authorized_level,
            "source_anchor_id": self.source_anchor_id,
            "source_anchor_digest": self.source_anchor_digest,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_payload(), "selection_digest": self.selection_digest}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AgencySelection":
        expected = {
            "selected_level",
            "selected_by",
            "maximum_authorized_level",
            "source_anchor_id",
            "source_anchor_digest",
            "selection_digest",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            _fail("invalid_agency_selection")
        return cls(**dict(value))


@dataclass(frozen=True)
class ContextRequirement:
    source_system: str
    purpose: str
    maximum_items: int
    maximum_bytes: int
    required: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_system",
            _validate_enum(
                self.source_system,
                CONTEXT_SOURCE_SYSTEMS,
                "invalid_context_source_system",
            ),
        )
        object.__setattr__(
            self,
            "purpose",
            _normalize_text(
                self.purpose,
                reason_code="invalid_context_purpose",
            ),
        )
        _require_int(
            self.maximum_items,
            minimum=1,
            maximum=_MAX_CONTEXT_ITEMS,
            reason_code="invalid_context_maximum_items",
        )
        _require_int(
            self.maximum_bytes,
            minimum=256,
            maximum=_MAX_CONTEXT_BYTES,
            reason_code="invalid_context_maximum_bytes",
        )
        _require_bool(self.required, "invalid_context_required")

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_system": self.source_system,
            "purpose": self.purpose,
            "maximum_items": self.maximum_items,
            "maximum_bytes": self.maximum_bytes,
            "required": self.required,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ContextRequirement":
        expected = {
            "source_system",
            "purpose",
            "maximum_items",
            "maximum_bytes",
            "required",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            _fail("invalid_context_requirement")
        return cls(**dict(value))


def default_context_requirements() -> tuple[ContextRequirement, ...]:
    """Return a bounded, source-labelled default without retrieving any data."""

    return (
        ContextRequirement(
            source_system="v7_memory",
            purpose="Retrieve only source-backed memory relevant to the objective",
            maximum_items=8,
            maximum_bytes=16_384,
            required=False,
        ),
        ContextRequirement(
            source_system="v9_now",
            purpose="Bind the current operational state without cached substitution",
            maximum_items=1,
            maximum_bytes=8_192,
            required=True,
        ),
        ContextRequirement(
            source_system="v9_resume",
            purpose="Bind the selected project resume package when available",
            maximum_items=1,
            maximum_bytes=16_384,
            required=False,
        ),
        ContextRequirement(
            source_system="project",
            purpose="Read only the project evidence needed for the stated goal",
            maximum_items=12,
            maximum_bytes=32_768,
            required=True,
        ),
    )


def _normalize_context_requirements(
    values: Sequence[ContextRequirement | Mapping[str, Any]],
) -> tuple[ContextRequirement, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        _fail("invalid_context_requirements")
    materialized: list[ContextRequirement] = []
    for value in values:
        if isinstance(value, ContextRequirement):
            materialized.append(value)
        elif isinstance(value, Mapping):
            materialized.append(ContextRequirement.from_dict(value))
        else:
            _fail("invalid_context_requirement")
    ordered = tuple(
        sorted(
            materialized,
            key=lambda item: (
                item.source_system,
                item.purpose,
                item.maximum_items,
                item.maximum_bytes,
                item.required,
            ),
        )
    )
    identities = [(item.source_system, item.purpose) for item in ordered]
    if len(identities) != len(set(identities)):
        _fail("duplicate_context_requirement")
    if sum(item.maximum_items for item in ordered) > _MAX_CONTEXT_ITEMS:
        _fail("context_item_budget_exceeded")
    if sum(item.maximum_bytes for item in ordered) > _MAX_CONTEXT_BYTES:
        _fail("context_byte_budget_exceeded")
    return ordered


@dataclass(frozen=True)
class AcceptancePolicy:
    mode: str = "explicit_user"
    automatic_allowed: bool = False
    requires_user_event: bool = True
    deterministic_criteria: tuple[str, ...] = ()
    maximum_risk: str = "low"
    require_system_verification: bool = True
    require_reversible: bool = True
    require_no_conflicts: bool = True
    require_no_overreach: bool = True
    require_no_unresolved_warnings: bool = True
    preauthorization_reference: str | None = None
    preauthorization_digest: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "mode",
            _validate_enum(
                self.mode,
                ("explicit_user", "preauthorized_deterministic"),
                "invalid_acceptance_mode",
            ),
        )
        automatic_allowed = _require_bool(
            self.automatic_allowed,
            "invalid_automatic_acceptance_flag",
        )
        requires_user_event = _require_bool(
            self.requires_user_event,
            "invalid_acceptance_user_event_flag",
        )
        object.__setattr__(
            self,
            "deterministic_criteria",
            _normalize_strings(
                self.deterministic_criteria,
                reason_code="invalid_acceptance_criterion",
            ),
        )
        object.__setattr__(
            self,
            "maximum_risk",
            _validate_enum(
                self.maximum_risk,
                ("low", "medium", "high"),
                "invalid_acceptance_maximum_risk",
            ),
        )
        if self.require_system_verification is not True:
            _fail("system_verification_required")
        for field_name, reason_code in (
            ("require_reversible", "automatic_acceptance_reversibility_required"),
            ("require_no_conflicts", "automatic_acceptance_conflict_gate_required"),
            ("require_no_overreach", "automatic_acceptance_overreach_gate_required"),
            (
                "require_no_unresolved_warnings",
                "automatic_acceptance_warning_gate_required",
            ),
        ):
            if getattr(self, field_name) is not True:
                _fail(reason_code)
        if self.mode == "explicit_user":
            if automatic_allowed or not requires_user_event:
                _fail("explicit_acceptance_requires_user_event")
            if (
                self.preauthorization_reference is not None
                or self.preauthorization_digest is not None
            ):
                _fail("explicit_acceptance_cannot_claim_preauthorization")
        if automatic_allowed:
            if (
                self.mode != "preauthorized_deterministic"
                or requires_user_event
                or self.maximum_risk != "low"
                or not self.deterministic_criteria
            ):
                _fail("unsafe_automatic_acceptance")
            if (
                self.preauthorization_reference is None
                or self.preauthorization_digest is None
            ):
                _fail("automatic_acceptance_preauthorization_required")
        if self.preauthorization_reference is not None:
            try:
                validate_identity(
                    self.preauthorization_reference,
                    "invalid_acceptance_preauthorization_reference",
                )
            except OperationalStateError as exc:
                raise ObjectiveIntentError(exc.reason_code) from None
        if self.preauthorization_digest is not None:
            try:
                validate_sha256(
                    self.preauthorization_digest,
                    "invalid_acceptance_preauthorization_digest",
                )
            except OperationalStateError as exc:
                raise ObjectiveIntentError(exc.reason_code) from None
        if (self.preauthorization_reference is None) != (
            self.preauthorization_digest is None
        ):
            _fail("incomplete_acceptance_preauthorization")

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "automatic_allowed": self.automatic_allowed,
            "requires_user_event": self.requires_user_event,
            "deterministic_criteria": list(self.deterministic_criteria),
            "maximum_risk": self.maximum_risk,
            "require_system_verification": self.require_system_verification,
            "require_reversible": self.require_reversible,
            "require_no_conflicts": self.require_no_conflicts,
            "require_no_overreach": self.require_no_overreach,
            "require_no_unresolved_warnings": self.require_no_unresolved_warnings,
            "preauthorization_reference": self.preauthorization_reference,
            "preauthorization_digest": self.preauthorization_digest,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AcceptancePolicy":
        expected = {
            "mode",
            "automatic_allowed",
            "requires_user_event",
            "deterministic_criteria",
            "maximum_risk",
            "require_system_verification",
            "require_reversible",
            "require_no_conflicts",
            "require_no_overreach",
            "require_no_unresolved_warnings",
            "preauthorization_reference",
            "preauthorization_digest",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            _fail("invalid_acceptance_policy")
        payload = dict(value)
        if not isinstance(payload["deterministic_criteria"], list):
            _fail("invalid_acceptance_policy")
        payload["deterministic_criteria"] = tuple(payload["deterministic_criteria"])
        return cls(**payload)


@dataclass(frozen=True)
class AuthorityRequest:
    requested: bool = False
    read_roots: tuple[str, ...] = ()
    write_roots: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    commands: tuple[str, ...] = ()
    risk_ceiling: str = "low"
    commit_permission: bool = False
    merge_permission: bool = False
    tag_permission: bool = False
    push_permission: bool = False
    network_permission: bool = False
    database_permission: str = "none"

    def __post_init__(self) -> None:
        _require_bool(self.requested, "invalid_authority_requested")
        object.__setattr__(
            self,
            "read_roots",
            _normalize_strings(
                self.read_roots,
                reason_code="invalid_authority_read_root",
                maximum=1024,
            ),
        )
        object.__setattr__(
            self,
            "write_roots",
            _normalize_strings(
                self.write_roots,
                reason_code="invalid_authority_write_root",
                maximum=1024,
            ),
        )
        object.__setattr__(
            self,
            "capabilities",
            _normalize_identities(
                self.capabilities,
                reason_code="invalid_authority_capability",
            ),
        )
        object.__setattr__(
            self,
            "commands",
            _normalize_strings(
                self.commands,
                reason_code="invalid_authority_command",
                maximum=1024,
            ),
        )
        object.__setattr__(
            self,
            "risk_ceiling",
            _validate_enum(
                self.risk_ceiling,
                RISK_CLASSES,
                "invalid_authority_risk_ceiling",
            ),
        )
        for name in (
            "commit_permission",
            "merge_permission",
            "tag_permission",
            "push_permission",
            "network_permission",
        ):
            _require_bool(getattr(self, name), f"invalid_{name}")
        object.__setattr__(
            self,
            "database_permission",
            _validate_enum(
                self.database_permission,
                ("none", "readonly", "write"),
                "invalid_database_permission",
            ),
        )

    @property
    def requests_write(self) -> bool:
        return bool(
            self.write_roots
            or self.capabilities
            or self.commands
            or self.commit_permission
            or self.merge_permission
            or self.tag_permission
            or self.push_permission
            or self.network_permission
            or self.database_permission == "write"
        )

    @property
    def grants_authority(self) -> bool:
        return False

    def read_only_copy(self, *, requested: bool = False) -> "AuthorityRequest":
        return replace(
            self,
            requested=requested,
            write_roots=(),
            capabilities=(),
            commands=(),
            risk_ceiling="low",
            commit_permission=False,
            merge_permission=False,
            tag_permission=False,
            push_permission=False,
            network_permission=False,
            database_permission=(
                "readonly" if self.database_permission == "readonly" else "none"
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested": self.requested,
            "read_roots": list(self.read_roots),
            "write_roots": list(self.write_roots),
            "capabilities": list(self.capabilities),
            "commands": list(self.commands),
            "risk_ceiling": self.risk_ceiling,
            "commit_permission": self.commit_permission,
            "merge_permission": self.merge_permission,
            "tag_permission": self.tag_permission,
            "push_permission": self.push_permission,
            "network_permission": self.network_permission,
            "database_permission": self.database_permission,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AuthorityRequest":
        expected = {
            "requested",
            "read_roots",
            "write_roots",
            "capabilities",
            "commands",
            "risk_ceiling",
            "commit_permission",
            "merge_permission",
            "tag_permission",
            "push_permission",
            "network_permission",
            "database_permission",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            _fail("invalid_authority_request")
        payload = dict(value)
        for field in ("read_roots", "write_roots", "capabilities", "commands"):
            if not isinstance(payload[field], list):
                _fail("invalid_authority_request")
            payload[field] = tuple(payload[field])
        return cls(**payload)


@dataclass(frozen=True)
class AmbiguitySignal:
    kind: str
    detail: str

    def __post_init__(self) -> None:
        if self.kind not in HARD_CLARIFICATION_KINDS | LOW_IMPACT_KINDS:
            _fail("invalid_ambiguity_kind")
        object.__setattr__(
            self,
            "detail",
            _normalize_text(
                self.detail,
                reason_code="invalid_ambiguity_detail",
            ),
        )

    @property
    def requires_clarification(self) -> bool:
        return self.kind in HARD_CLARIFICATION_KINDS

    def reason(self) -> str:
        return f"{self.kind}:{self.detail}"


@dataclass(frozen=True)
class AmbiguityAssessment:
    hard_reasons: tuple[str, ...]
    low_impact_reasons: tuple[str, ...]
    clarification_required: bool
    readonly_work_may_continue: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "hard_reasons",
            _normalize_strings(
                self.hard_reasons,
                reason_code="invalid_hard_ambiguity_reason",
            ),
        )
        object.__setattr__(
            self,
            "low_impact_reasons",
            _normalize_strings(
                self.low_impact_reasons,
                reason_code="invalid_low_impact_reason",
            ),
        )
        if self.clarification_required != bool(self.hard_reasons):
            _fail("clarification_assessment_mismatch")
        if self.readonly_work_may_continue is not True:
            _fail("readonly_progress_boundary_required")

    def to_dict(self) -> dict[str, Any]:
        return {
            "hard_reasons": list(self.hard_reasons),
            "low_impact_reasons": list(self.low_impact_reasons),
            "clarification_required": self.clarification_required,
            "readonly_work_may_continue": self.readonly_work_may_continue,
        }


def _coerce_ambiguity_signal(
    value: AmbiguitySignal | Mapping[str, Any],
) -> AmbiguitySignal:
    if isinstance(value, AmbiguitySignal):
        return value
    if isinstance(value, Mapping) and set(value) == {"kind", "detail"}:
        return AmbiguitySignal(kind=value["kind"], detail=value["detail"])
    _fail("invalid_ambiguity_signal")


def classify_ambiguities(
    signals: Sequence[AmbiguitySignal | Mapping[str, Any]] = (),
    *,
    success_criteria: Sequence[str] = (),
) -> AmbiguityAssessment:
    """Classify explicit evidence; no model, I/O, or keyword guessing occurs."""

    if isinstance(signals, (str, bytes)) or not isinstance(signals, Sequence):
        _fail("invalid_ambiguity_signals")
    if len(signals) > _MAX_STRING_ITEMS:
        _fail("objective_collection_limit_exceeded")
    normalized_criteria = _normalize_strings(
        success_criteria,
        reason_code="invalid_success_criterion",
    )
    materialized = [_coerce_ambiguity_signal(value) for value in signals]
    if not normalized_criteria:
        materialized.append(
            AmbiguitySignal(
                kind="missing_success_criteria",
                detail="No verifiable success criterion was supplied",
            )
        )
    hard = tuple(
        signal.reason() for signal in materialized if signal.requires_clarification
    )
    low = tuple(
        signal.reason() for signal in materialized if not signal.requires_clarification
    )
    return AmbiguityAssessment(
        hard_reasons=hard,
        low_impact_reasons=low,
        clarification_required=bool(hard),
        readonly_work_may_continue=True,
    )


def resolve_agency_level(
    *,
    selected_level: str = "consult",
    selected_by: str = "default",
    maximum_authorized_level: str = "act",
    agent_proposed_level: str | None = None,
) -> str:
    """Resolve only a trusted user/default/rule selection; agents cannot elevate it."""

    selected_level = _validate_enum(
        selected_level,
        AGENCY_LEVELS,
        "invalid_agency_level",
    )
    maximum_authorized_level = _validate_enum(
        maximum_authorized_level,
        AGENCY_LEVELS,
        "invalid_maximum_agency_level",
    )
    if selected_by not in {"default", "user", "preauthorized_rule"}:
        _fail("untrusted_agency_selector")
    if selected_by == "default" and selected_level != "consult":
        _fail("default_agency_must_be_consult")
    if _AGENCY_RANK[selected_level] > _AGENCY_RANK[maximum_authorized_level]:
        _fail("agency_level_exceeds_authorized_maximum")
    if agent_proposed_level is not None:
        agent_proposed_level = _validate_enum(
            agent_proposed_level,
            AGENCY_LEVELS,
            "invalid_agent_proposed_agency_level",
        )
        if _AGENCY_RANK[agent_proposed_level] > _AGENCY_RANK[selected_level]:
            _fail("agent_agency_escalation_denied")
    return selected_level


@dataclass(frozen=True)
class IntentResolution:
    user_utterance: str
    interpreted_goal: str
    agency_level: str
    cognitive_stance: str
    success_criteria: tuple[str, ...]
    prohibited_outcomes: tuple[str, ...]
    ambiguity_assessment: AmbiguityAssessment
    resolution_digest: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "user_utterance",
            _normalize_text(
                self.user_utterance,
                reason_code="invalid_user_utterance",
            ),
        )
        object.__setattr__(
            self,
            "interpreted_goal",
            _normalize_text(
                self.interpreted_goal,
                reason_code="invalid_interpreted_goal",
            ),
        )
        object.__setattr__(
            self,
            "agency_level",
            _validate_enum(
                self.agency_level,
                AGENCY_LEVELS,
                "invalid_agency_level",
            ),
        )
        object.__setattr__(
            self,
            "cognitive_stance",
            _validate_enum(
                self.cognitive_stance,
                COGNITIVE_STANCES,
                "invalid_cognitive_stance",
            ),
        )
        object.__setattr__(
            self,
            "success_criteria",
            _normalize_strings(
                self.success_criteria,
                reason_code="invalid_success_criterion",
            ),
        )
        object.__setattr__(
            self,
            "prohibited_outcomes",
            _normalize_strings(
                self.prohibited_outcomes,
                reason_code="invalid_prohibited_outcome",
            ),
        )
        expected = canonical_digest(self.identity_payload())
        if self.resolution_digest and self.resolution_digest != expected:
            _fail("intent_resolution_digest_mismatch")
        object.__setattr__(self, "resolution_digest", expected)

    @property
    def contract_ready(self) -> bool:
        return bool(self.success_criteria)

    def identity_payload(self) -> dict[str, Any]:
        return {
            "user_utterance": self.user_utterance,
            "interpreted_goal": self.interpreted_goal,
            "agency_level": self.agency_level,
            "cognitive_stance": self.cognitive_stance,
            "success_criteria": list(self.success_criteria),
            "prohibited_outcomes": list(self.prohibited_outcomes),
            "ambiguity_assessment": self.ambiguity_assessment.to_dict(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.identity_payload(),
            "contract_ready": self.contract_ready,
            "resolution_digest": self.resolution_digest,
        }


def resolve_objective_intent(
    *,
    user_utterance: str,
    interpreted_goal: str | None = None,
    agency_level: str = "consult",
    agency_selected_by: str = "default",
    maximum_authorized_agency_level: str = "act",
    agent_proposed_agency_level: str | None = None,
    cognitive_stance: str = "continue",
    success_criteria: Sequence[str] = (),
    prohibited_outcomes: Sequence[str] = (),
    ambiguity_signals: Sequence[AmbiguitySignal | Mapping[str, Any]] = (),
) -> IntentResolution:
    normalized_utterance = _normalize_text(
        user_utterance,
        reason_code="invalid_user_utterance",
    )
    normalized_goal = _normalize_text(
        interpreted_goal if interpreted_goal is not None else normalized_utterance,
        reason_code="invalid_interpreted_goal",
    )
    normalized_criteria = _normalize_strings(
        success_criteria,
        reason_code="invalid_success_criterion",
    )
    selected_agency = resolve_agency_level(
        selected_level=agency_level,
        selected_by=agency_selected_by,
        maximum_authorized_level=maximum_authorized_agency_level,
        agent_proposed_level=agent_proposed_agency_level,
    )
    stance = _validate_enum(
        cognitive_stance,
        COGNITIVE_STANCES,
        "invalid_cognitive_stance",
    )
    assessment = classify_ambiguities(
        ambiguity_signals,
        success_criteria=normalized_criteria,
    )
    custom_prohibited = _normalize_strings(
        prohibited_outcomes,
        reason_code="invalid_prohibited_outcome",
    )
    normalized_prohibited = _normalize_strings(
        (*DEFAULT_PROHIBITED_OUTCOMES, *custom_prohibited),
        reason_code="invalid_prohibited_outcome",
    )
    return IntentResolution(
        user_utterance=normalized_utterance,
        interpreted_goal=normalized_goal,
        agency_level=selected_agency,
        cognitive_stance=stance,
        success_criteria=normalized_criteria,
        prohibited_outcomes=normalized_prohibited,
        ambiguity_assessment=assessment,
    )


@dataclass(frozen=True)
class ObjectiveContract:
    objective_id: str
    user_utterance: str
    interpreted_goal: str
    project_id: str
    agency_level: str
    agency_selection: AgencySelection
    cognitive_stance: str
    success_criteria: tuple[str, ...]
    prohibited_outcomes: tuple[str, ...]
    risk_tolerance: str
    time_constraints: tuple[str, ...]
    resource_constraints: tuple[str, ...]
    assumptions: tuple[str, ...]
    ambiguities: tuple[str, ...]
    clarification_required: bool
    context_requirements: tuple[ContextRequirement, ...]
    acceptance_policy: AcceptancePolicy
    authority_request: AuthorityRequest
    source_anchors: tuple[ObjectiveSourceAnchor, ...]
    canonical_digest: str = ""
    version: int = OBJECTIVE_CONTRACT_VERSION

    def __post_init__(self) -> None:
        try:
            validate_identity(self.objective_id, "invalid_objective_id")
            validate_identity(self.project_id, "invalid_objective_project_id")
        except OperationalStateError as exc:
            raise ObjectiveIntentError(exc.reason_code) from None
        _reject_secret_literal(self.objective_id)
        _reject_secret_literal(self.project_id)
        object.__setattr__(
            self,
            "user_utterance",
            _normalize_text(
                self.user_utterance,
                reason_code="invalid_user_utterance",
            ),
        )
        object.__setattr__(
            self,
            "interpreted_goal",
            _normalize_text(
                self.interpreted_goal,
                reason_code="invalid_interpreted_goal",
            ),
        )
        object.__setattr__(
            self,
            "agency_level",
            _validate_enum(
                self.agency_level,
                AGENCY_LEVELS,
                "invalid_agency_level",
            ),
        )
        if not isinstance(self.agency_selection, AgencySelection):
            _fail("invalid_agency_selection")
        if self.agency_selection.selected_level != self.agency_level:
            _fail("agency_selection_contract_mismatch")
        object.__setattr__(
            self,
            "cognitive_stance",
            _validate_enum(
                self.cognitive_stance,
                COGNITIVE_STANCES,
                "invalid_cognitive_stance",
            ),
        )
        object.__setattr__(
            self,
            "success_criteria",
            _normalize_strings(
                self.success_criteria,
                reason_code="invalid_success_criterion",
            ),
        )
        if not self.success_criteria:
            _fail("objective_success_criteria_required")
        for name, reason in (
            ("prohibited_outcomes", "invalid_prohibited_outcome"),
            ("time_constraints", "invalid_time_constraint"),
            ("resource_constraints", "invalid_resource_constraint"),
            ("assumptions", "invalid_objective_assumption"),
            ("ambiguities", "invalid_objective_ambiguity"),
        ):
            object.__setattr__(
                self,
                name,
                _normalize_strings(getattr(self, name), reason_code=reason),
            )
        object.__setattr__(
            self,
            "risk_tolerance",
            _validate_enum(
                self.risk_tolerance,
                RISK_CLASSES,
                "invalid_risk_tolerance",
            ),
        )
        _require_bool(
            self.clarification_required,
            "invalid_clarification_required",
        )
        if self.clarification_required != bool(self.ambiguities):
            _fail("clarification_contract_mismatch")
        object.__setattr__(
            self,
            "context_requirements",
            _normalize_context_requirements(self.context_requirements),
        )
        if not isinstance(self.acceptance_policy, AcceptancePolicy):
            _fail("invalid_acceptance_policy")
        if not isinstance(self.authority_request, AuthorityRequest):
            _fail("invalid_authority_request")
        if isinstance(self.source_anchors, (str, bytes)) or not isinstance(
            self.source_anchors,
            Sequence,
        ):
            _fail("invalid_objective_source_anchors")
        if not 1 <= len(self.source_anchors) <= 64:
            _fail("invalid_objective_source_anchors")
        anchors: list[ObjectiveSourceAnchor] = []
        for anchor in self.source_anchors:
            anchors.append(_validate_objective_source_anchor(anchor))
        if not anchors:
            _fail("objective_source_anchor_required")
        ordered_anchors = tuple(
            sorted(
                anchors,
                key=lambda item: (
                    item.anchor_id,
                    item.evidence_digest,
                    item.observed_at,
                ),
            )
        )
        if len({anchor.anchor_id for anchor in ordered_anchors}) != len(
            ordered_anchors
        ):
            _fail("duplicate_objective_source_anchor")
        object.__setattr__(self, "source_anchors", ordered_anchors)
        anchors_by_id = {anchor.anchor_id: anchor for anchor in ordered_anchors}
        if self.agency_selection.source_anchor_id is not None:
            selection_anchor = anchors_by_id.get(
                self.agency_selection.source_anchor_id
            )
            if selection_anchor is None:
                _fail("agency_selection_source_anchor_missing")
            if (
                selection_anchor.evidence_digest
                != self.agency_selection.source_anchor_digest
            ):
                _fail("agency_selection_source_digest_mismatch")
            allowed_selection_kinds = (
                {"user_declaration"}
                if self.agency_selection.selected_by == "user"
                else {"approved_memory", "runtime"}
            )
            if selection_anchor.source_kind not in allowed_selection_kinds:
                _fail("agency_selection_source_kind_denied")
        if self.acceptance_policy.automatic_allowed:
            if self.risk_tolerance != "low":
                _fail("automatic_acceptance_objective_risk_denied")
            acceptance_anchor = anchors_by_id.get(
                self.acceptance_policy.preauthorization_reference
            )
            if acceptance_anchor is None:
                _fail("acceptance_preauthorization_anchor_missing")
            if (
                acceptance_anchor.evidence_digest
                != self.acceptance_policy.preauthorization_digest
            ):
                _fail("acceptance_preauthorization_digest_mismatch")
            if acceptance_anchor.source_kind not in {
                "user_declaration",
                "approved_memory",
                "runtime",
            }:
                _fail("acceptance_preauthorization_source_kind_denied")
        for anchor in ordered_anchors:
            _reject_secret_literal(anchor.anchor_id)
            _normalize_text(
                anchor.locator,
                reason_code="invalid_objective_anchor_locator",
                maximum=512,
            )
        _require_int(
            self.version,
            minimum=1,
            reason_code="invalid_objective_version",
        )
        if self.agency_level in {"consult", "plan"}:
            if self.authority_request.requests_write:
                _fail("consult_plan_write_authority_request_denied")
        if self.agency_level in {"consult", "plan"}:
            if self.authority_request.write_roots:
                _fail("consult_plan_write_scope_denied")
        if (
            self.agency_level == "act"
            and not self.clarification_required
            and not self.authority_request.requested
        ):
            _fail("act_authority_request_required")
        if self.clarification_required and self.authority_request.requests_write:
            _fail("clarification_write_request_denied")
        expected = canonical_digest(self.identity_payload())
        if self.canonical_digest and self.canonical_digest != expected:
            _fail("objective_digest_mismatch")
        object.__setattr__(self, "canonical_digest", expected)

    @property
    def grants_execution_authority(self) -> bool:
        return False

    @property
    def execution_ready(self) -> bool:
        return False

    def identity_payload(self) -> dict[str, Any]:
        return {
            "objective_id": self.objective_id,
            "user_utterance": self.user_utterance,
            "interpreted_goal": self.interpreted_goal,
            "project_id": self.project_id,
            "agency_level": self.agency_level,
            "agency_selection": self.agency_selection.to_dict(),
            "cognitive_stance": self.cognitive_stance,
            "success_criteria": list(self.success_criteria),
            "prohibited_outcomes": list(self.prohibited_outcomes),
            "risk_tolerance": self.risk_tolerance,
            "time_constraints": list(self.time_constraints),
            "resource_constraints": list(self.resource_constraints),
            "assumptions": list(self.assumptions),
            "ambiguities": list(self.ambiguities),
            "clarification_required": self.clarification_required,
            "context_requirements": [
                requirement.to_dict() for requirement in self.context_requirements
            ],
            "acceptance_policy": self.acceptance_policy.to_dict(),
            "authority_request": self.authority_request.to_dict(),
            "source_anchors": [
                anchor.to_dict() for anchor in self.source_anchors
            ],
            "version": self.version,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self.identity_payload()
        payload["canonical_digest"] = self.canonical_digest
        return payload

    def to_envelope(self) -> dict[str, Any]:
        return {
            "schema_generation": COGNITIVE_SCHEMA_GENERATION,
            "document_type": "objective_contract",
            "document": self.to_dict(),
        }

    def to_canonical_json(self) -> str:
        return canonical_json(self.to_envelope())

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ObjectiveContract":
        expected = {
            "objective_id",
            "user_utterance",
            "interpreted_goal",
            "project_id",
            "agency_level",
            "agency_selection",
            "cognitive_stance",
            "success_criteria",
            "prohibited_outcomes",
            "risk_tolerance",
            "time_constraints",
            "resource_constraints",
            "assumptions",
            "ambiguities",
            "clarification_required",
            "context_requirements",
            "acceptance_policy",
            "authority_request",
            "source_anchors",
            "canonical_digest",
            "version",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            _fail("invalid_objective_contract")
        payload = dict(value)
        for field in (
            "success_criteria",
            "prohibited_outcomes",
            "time_constraints",
            "resource_constraints",
            "assumptions",
            "ambiguities",
            "context_requirements",
            "source_anchors",
        ):
            if not isinstance(payload[field], list):
                _fail("invalid_objective_contract")
        for field in (
            "success_criteria",
            "prohibited_outcomes",
            "time_constraints",
            "resource_constraints",
            "assumptions",
            "ambiguities",
        ):
            payload[field] = tuple(payload[field])
        payload["context_requirements"] = tuple(
            ContextRequirement.from_dict(item)
            for item in payload["context_requirements"]
        )
        payload["acceptance_policy"] = AcceptancePolicy.from_dict(
            payload["acceptance_policy"]
        )
        payload["agency_selection"] = AgencySelection.from_dict(
            payload["agency_selection"]
        )
        payload["authority_request"] = AuthorityRequest.from_dict(
            payload["authority_request"]
        )
        try:
            payload["source_anchors"] = tuple(
                ObjectiveSourceAnchor.from_dict(item)
                for item in payload["source_anchors"]
            )
        except OperationalStateError as exc:
            raise ObjectiveIntentError(exc.reason_code) from None
        contract = cls(**payload)
        if len(contract.to_canonical_json().encode("utf-8")) > _MAX_CONTRACT_BYTES:
            _fail("objective_contract_byte_budget_exceeded")
        return contract


def build_objective_contract(
    *,
    user_utterance: str,
    project_id: str,
    source_anchors: Sequence[ObjectiveSourceAnchor | Mapping[str, Any]],
    success_criteria: Sequence[str],
    interpreted_goal: str | None = None,
    agency_level: str = "consult",
    agency_selected_by: str = "default",
    maximum_authorized_agency_level: str = "act",
    agent_proposed_agency_level: str | None = None,
    agency_selection_source_anchor_id: str | None = None,
    cognitive_stance: str = "continue",
    prohibited_outcomes: Sequence[str] = (),
    ambiguity_signals: Sequence[AmbiguitySignal | Mapping[str, Any]] = (),
    risk_tolerance: str = "low",
    time_constraints: Sequence[str] = (),
    resource_constraints: Sequence[str] = (),
    assumptions: Sequence[str] = (),
    context_requirements: Sequence[
        ContextRequirement | Mapping[str, Any]
    ] | None = None,
    acceptance_policy: AcceptancePolicy | None = None,
    authority_request: AuthorityRequest | None = None,
    objective_id: str | None = None,
    version: int = OBJECTIVE_CONTRACT_VERSION,
) -> ObjectiveContract:
    """Build an immutable contract. This function cannot execute or persist work."""

    resolution = resolve_objective_intent(
        user_utterance=user_utterance,
        interpreted_goal=interpreted_goal,
        agency_level=agency_level,
        agency_selected_by=agency_selected_by,
        maximum_authorized_agency_level=maximum_authorized_agency_level,
        agent_proposed_agency_level=agent_proposed_agency_level,
        cognitive_stance=cognitive_stance,
        success_criteria=success_criteria,
        prohibited_outcomes=prohibited_outcomes,
        ambiguity_signals=ambiguity_signals,
    )
    if not resolution.contract_ready:
        _fail("success_criteria_required_before_contract")
    try:
        validate_identity(project_id, "invalid_objective_project_id")
    except OperationalStateError as exc:
        raise ObjectiveIntentError(exc.reason_code) from None
    normalized_anchors: list[ObjectiveSourceAnchor] = []
    if (
        isinstance(source_anchors, (str, bytes))
        or not isinstance(source_anchors, Sequence)
        or not 1 <= len(source_anchors) <= 64
    ):
        _fail("invalid_objective_source_anchors")
    for anchor in source_anchors:
        if isinstance(anchor, ObjectiveSourceAnchor):
            normalized_anchors.append(
                _validate_objective_source_anchor(anchor)
            )
        elif isinstance(anchor, Mapping):
            try:
                normalized_anchors.append(
                    _validate_objective_source_anchor(
                        ObjectiveSourceAnchor.from_dict(anchor)
                    )
                )
            except OperationalStateError as exc:
                raise ObjectiveIntentError(exc.reason_code) from None
        else:
            _fail("invalid_objective_source_anchor")
    selection_anchor: ObjectiveSourceAnchor | None = None
    if agency_selected_by != "default":
        allowed_kinds = (
            {"user_declaration"}
            if agency_selected_by == "user"
            else {"approved_memory", "runtime"}
        )
        matching = [
            anchor
            for anchor in normalized_anchors
            if anchor.source_kind in allowed_kinds
            and (
                agency_selection_source_anchor_id is None
                or anchor.anchor_id == agency_selection_source_anchor_id
            )
        ]
        if len(matching) != 1:
            _fail("agency_selection_source_required")
        selection_anchor = matching[0]
    elif agency_selection_source_anchor_id is not None:
        _fail("default_agency_cannot_claim_authority_source")
    agency_selection = AgencySelection(
        selected_level=resolution.agency_level,
        selected_by=agency_selected_by,
        maximum_authorized_level=(
            "consult"
            if agency_selected_by == "default"
            else maximum_authorized_agency_level
        ),
        source_anchor_id=(
            selection_anchor.anchor_id if selection_anchor is not None else None
        ),
        source_anchor_digest=(
            selection_anchor.evidence_digest
            if selection_anchor is not None
            else None
        ),
    )
    normalized_context = _normalize_context_requirements(
        tuple(context_requirements)
        if context_requirements is not None
        else default_context_requirements()
    )
    selected_acceptance = acceptance_policy or AcceptancePolicy()
    selected_authority = authority_request
    if selected_authority is None:
        selected_authority = AuthorityRequest(
            requested=resolution.agency_level == "act"
            and not resolution.ambiguity_assessment.clarification_required,
            risk_ceiling=risk_tolerance,
        )
    if (
        resolution.agency_level in {"consult", "plan"}
        or resolution.ambiguity_assessment.clarification_required
    ):
        selected_authority = selected_authority.read_only_copy(requested=False)
    seed = {
        "user_utterance": resolution.user_utterance,
        "interpreted_goal": resolution.interpreted_goal,
        "project_id": project_id,
        "agency_level": resolution.agency_level,
        "agency_selection": agency_selection.to_dict(),
        "cognitive_stance": resolution.cognitive_stance,
        "success_criteria": list(resolution.success_criteria),
        "prohibited_outcomes": list(resolution.prohibited_outcomes),
        "risk_tolerance": risk_tolerance,
        "time_constraints": list(
            _normalize_strings(
                time_constraints,
                reason_code="invalid_time_constraint",
            )
        ),
        "resource_constraints": list(
            _normalize_strings(
                resource_constraints,
                reason_code="invalid_resource_constraint",
            )
        ),
        "assumptions": list(
            _normalize_strings(
                assumptions,
                reason_code="invalid_objective_assumption",
            )
        ),
        "ambiguities": list(
            resolution.ambiguity_assessment.hard_reasons
        ),
        "clarification_required": (
            resolution.ambiguity_assessment.clarification_required
        ),
        "context_requirements": [
            item.to_dict() for item in normalized_context
        ],
        "acceptance_policy": selected_acceptance.to_dict(),
        "authority_request": selected_authority.to_dict(),
        "source_anchors": [
            anchor.to_dict()
            for anchor in sorted(
                normalized_anchors,
                key=lambda item: (
                    item.anchor_id,
                    item.evidence_digest,
                    item.observed_at,
                ),
            )
        ],
        "version": version,
    }
    selected_id = objective_id or make_identity("objective", seed)
    contract = ObjectiveContract(
        objective_id=selected_id,
        user_utterance=resolution.user_utterance,
        interpreted_goal=resolution.interpreted_goal,
        project_id=project_id,
        agency_level=resolution.agency_level,
        agency_selection=agency_selection,
        cognitive_stance=resolution.cognitive_stance,
        success_criteria=resolution.success_criteria,
        prohibited_outcomes=resolution.prohibited_outcomes,
        risk_tolerance=risk_tolerance,
        time_constraints=tuple(time_constraints),
        resource_constraints=tuple(resource_constraints),
        assumptions=tuple(assumptions),
        ambiguities=resolution.ambiguity_assessment.hard_reasons,
        clarification_required=(
            resolution.ambiguity_assessment.clarification_required
        ),
        context_requirements=normalized_context,
        acceptance_policy=selected_acceptance,
        authority_request=selected_authority,
        source_anchors=tuple(normalized_anchors),
        version=version,
    )
    if len(contract.to_canonical_json().encode("utf-8")) > _MAX_CONTRACT_BYTES:
        _fail("objective_contract_byte_budget_exceeded")
    return contract
