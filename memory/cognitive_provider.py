"""Pure provider registry and adapter contracts for YunOS v10-C.

This module deliberately has no environment, file, database, credential, or
network access.  It describes providers and validates a caller's explicit
manual selection.  Runtime execution remains the responsibility of the v8
Runtime boundary.
"""
from __future__ import annotations

from dataclasses import InitVar, dataclass
from datetime import datetime
import re
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Protocol, Sequence, runtime_checkable
from urllib.parse import urlsplit

from memory.operational_state_models import (
    OperationalStateError,
    canonical_digest,
    normalize_timestamp,
    validate_identity,
    validate_sha256,
)


PROVIDER_CONTRACT_VERSION = 1
ADAPTER_KINDS = (
    "deepseek_v8_runtime",
    "openai_compatible",
    "local_openai_compatible",
    "fake",
)
TOOL_CALLING_SUPPORT = ("none", "compatible", "native")
STRUCTURED_OUTPUT_SUPPORT = ("none", "json_object", "json_schema")
HEALTH_STATES = ("healthy", "degraded", "unknown", "unavailable", "disabled")
HEALTH_REASONS = (
    "ready",
    "health_not_checked",
    "credential_reference_unavailable",
    "profile_disabled",
    "provider_degraded",
    "provider_unavailable",
    "test_only",
)
REASONING_OPTIONS = (
    "disabled",
    "provider_default",
    "low",
    "medium",
    "high",
)
CAPABILITY_FLAGS = (
    "chat",
    "reasoning",
    "structured_output",
    "tool_proposals",
    "usage_reporting",
    "local_only",
    "deterministic_fake",
)
RESOLVER_KINDS = (
    "v8_private_file",
    "environment",
    "os_keychain",
    "local_broker",
)
PRICING_STATES = ("known", "unknown", "not_applicable")
RESPONSE_MODES = ("text", "json_object", "json_schema")
ATTESTED_HEALTH_STATES = ("healthy", "degraded", "unavailable")
MAX_HEALTH_ATTESTATION_SECONDS = 900

_PROVIDER_SELECTION_FACTORY_SEAL = object()

_MAX_CONTEXT_TOKENS = 4_194_304
_MAX_TIMEOUT_MS = 1_200_000
_MAX_TEXT_CHARS = 131_072
_MAX_OUTPUT_TOKENS = 262_144
_MAX_COLLECTION_ITEMS = 64
_REFERENCE_RE = re.compile(r"^secret\.[a-z][a-z0-9_.-]{2,127}$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}$")
_ADAPTER_VERSION_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SECRET_FIELD_RE = re.compile(
    r"(?i)(?:^|[_-])(?:api[_-]?key|access[_-]?token|authorization|"
    r"credential|password|private[_-]?key|secret|token|value)(?:$|[_-])"
)
_SECRET_LITERAL_PATTERNS = (
    re.compile(r"(?i)\bsk-(?:proj-)?[a-z0-9_-]{12,}\b"),
    re.compile(r"(?i)\bBearer\s+[a-z0-9._~+/=-]{10,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(
        r"(?i)(?:api[_-]?key|access[_-]?token|authorization|credential|"
        r"password|private[_-]?key|secret)\s*[:=]\s*[^\s\"'},;]{8,}"
    ),
)


class CognitiveProviderError(OperationalStateError):
    """Fail-closed v10-C provider contract error."""


def _fail(reason_code: str) -> None:
    raise CognitiveProviderError(reason_code)


def _text(value: Any, *, reason_code: str, maximum: int = 512) -> str:
    if not isinstance(value, str):
        _fail(reason_code)
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > maximum
        or "\x00" in normalized
        or any(ord(character) < 32 and character not in "\n\t" for character in normalized)
    ):
        _fail(reason_code)
    if any(pattern.search(normalized) for pattern in _SECRET_LITERAL_PATTERNS):
        _fail("provider_secret_material_forbidden")
    return normalized


def _identity(value: Any, reason_code: str) -> str:
    try:
        return validate_identity(value, reason_code)
    except OperationalStateError as exc:
        raise CognitiveProviderError(exc.reason_code) from None


def _sha256(value: Any, reason_code: str) -> str:
    try:
        return validate_sha256(value, reason_code)
    except OperationalStateError as exc:
        raise CognitiveProviderError(exc.reason_code) from None


def _timestamp(value: Any, reason_code: str) -> str:
    try:
        return normalize_timestamp(value)
    except OperationalStateError:
        _fail(reason_code)


def _timestamp_value(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _integer(
    value: Any,
    *,
    minimum: int,
    maximum: int,
    reason_code: str,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        _fail(reason_code)
    return value


def _enum(value: Any, allowed: Sequence[str], reason_code: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        _fail(reason_code)
    return value


def _strings(
    values: Iterable[str],
    *,
    allowed: Sequence[str],
    reason_code: str,
    require_nonempty: bool = False,
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        _fail(reason_code)
    materialized = list(values)
    if len(materialized) > _MAX_COLLECTION_ITEMS:
        _fail(reason_code)
    normalized: set[str] = set()
    for value in materialized:
        normalized.add(_enum(value, allowed, reason_code))
    if require_nonempty and not normalized:
        _fail(reason_code)
    return tuple(sorted(normalized))


def _strict_keys(
    value: Any,
    expected: set[str],
    reason_code: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected:
        _fail(reason_code)
    for key in value:
        if _SECRET_FIELD_RE.search(str(key)) and key != "secret_reference":
            _fail("provider_secret_field_forbidden")
    return value


def _endpoint(value: Any, adapter_kind: str) -> str:
    endpoint = _text(value, reason_code="invalid_provider_endpoint", maximum=2048)
    parsed = urlsplit(endpoint)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        _fail("unsafe_provider_endpoint")
    if adapter_kind == "fake":
        if parsed.scheme != "fake" or parsed.netloc != "contract":
            _fail("unsafe_provider_endpoint")
        return endpoint.rstrip("/")
    if adapter_kind == "local_openai_compatible":
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or not parsed.netloc
        ):
            _fail("unsafe_local_provider_endpoint")
        return endpoint.rstrip("/")
    if parsed.scheme != "https" or not parsed.netloc:
        _fail("unsafe_provider_endpoint")
    return endpoint.rstrip("/")


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


@dataclass(frozen=True)
class SecretReference:
    """Opaque resolver pointer.  It can never carry credential material."""

    reference_id: str
    resolver_kind: str
    required: bool = True

    def __post_init__(self) -> None:
        reference_id = _text(
            self.reference_id,
            reason_code="invalid_provider_secret_reference",
            maximum=128,
        )
        if _REFERENCE_RE.fullmatch(reference_id) is None:
            _fail("invalid_provider_secret_reference")
        resolver_kind = _enum(
            self.resolver_kind,
            RESOLVER_KINDS,
            "invalid_provider_secret_resolver",
        )
        if not isinstance(self.required, bool):
            _fail("invalid_provider_secret_required")
        object.__setattr__(self, "reference_id", reference_id)
        object.__setattr__(self, "resolver_kind", resolver_kind)

    def to_dict(self) -> dict[str, Any]:
        return {
            "reference_id": self.reference_id,
            "resolver_kind": self.resolver_kind,
            "required": self.required,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SecretReference":
        payload = _strict_keys(
            value,
            {"reference_id", "resolver_kind", "required"},
            "invalid_provider_secret_reference",
        )
        return cls(**dict(payload))


@dataclass(frozen=True)
class CostMetadata:
    """Stable pricing metadata; values are integer microunits, never floats."""

    pricing_state: str
    currency: str | None
    input_microunits_per_million_tokens: int | None
    output_microunits_per_million_tokens: int | None

    def __post_init__(self) -> None:
        state = _enum(
            self.pricing_state,
            PRICING_STATES,
            "invalid_provider_pricing_state",
        )
        if state == "known":
            if self.currency not in {"USD", "CNY"}:
                _fail("invalid_provider_pricing_currency")
            for value in (
                self.input_microunits_per_million_tokens,
                self.output_microunits_per_million_tokens,
            ):
                _integer(
                    value,
                    minimum=0,
                    maximum=10**15,
                    reason_code="invalid_provider_pricing_value",
                )
        elif state == "unknown":
            if (
                self.currency not in {"USD", "CNY"}
                or self.input_microunits_per_million_tokens is not None
                or self.output_microunits_per_million_tokens is not None
            ):
                _fail("invalid_unknown_provider_pricing")
        else:
            if (
                self.currency is not None
                or self.input_microunits_per_million_tokens is not None
                or self.output_microunits_per_million_tokens is not None
            ):
                _fail("invalid_not_applicable_provider_pricing")
        object.__setattr__(self, "pricing_state", state)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pricing_state": self.pricing_state,
            "currency": self.currency,
            "input_microunits_per_million_tokens": (
                self.input_microunits_per_million_tokens
            ),
            "output_microunits_per_million_tokens": (
                self.output_microunits_per_million_tokens
            ),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CostMetadata":
        payload = _strict_keys(
            value,
            {
                "pricing_state",
                "currency",
                "input_microunits_per_million_tokens",
                "output_microunits_per_million_tokens",
            },
            "invalid_provider_cost_metadata",
        )
        return cls(**dict(payload))


@dataclass(frozen=True)
class ProviderProfile:
    provider_id: str
    display_name: str
    profile_version: int
    adapter_kind: str
    adapter_version: str
    endpoint: str
    model: str
    capability_flags: tuple[str, ...]
    tool_calling_support: str
    structured_output_support: str
    context_limit_tokens: int
    timeout_ms: int
    reasoning_options: tuple[str, ...]
    cost_metadata: CostMetadata
    enabled: bool
    secret_reference: SecretReference | None
    health_state: str
    health_reason: str
    profile_digest: str = ""

    def __post_init__(self) -> None:
        provider_id = _identity(self.provider_id, "invalid_provider_id")
        display_name = _text(
            self.display_name,
            reason_code="invalid_provider_display_name",
            maximum=128,
        )
        profile_version = _integer(
            self.profile_version,
            minimum=1,
            maximum=2_147_483_647,
            reason_code="invalid_provider_profile_version",
        )
        adapter_kind = _enum(
            self.adapter_kind,
            ADAPTER_KINDS,
            "invalid_provider_adapter_kind",
        )
        adapter_version = _text(
            self.adapter_version,
            reason_code="invalid_provider_adapter_version",
            maximum=64,
        )
        if _ADAPTER_VERSION_RE.fullmatch(adapter_version) is None:
            _fail("invalid_provider_adapter_version")
        endpoint = _endpoint(self.endpoint, adapter_kind)
        model = _text(
            self.model,
            reason_code="invalid_provider_model",
            maximum=128,
        )
        if _MODEL_RE.fullmatch(model) is None:
            _fail("invalid_provider_model")
        capability_flags = _strings(
            self.capability_flags,
            allowed=CAPABILITY_FLAGS,
            reason_code="invalid_provider_capability_flags",
            require_nonempty=True,
        )
        tool_support = _enum(
            self.tool_calling_support,
            TOOL_CALLING_SUPPORT,
            "invalid_provider_tool_calling_support",
        )
        structured_support = _enum(
            self.structured_output_support,
            STRUCTURED_OUTPUT_SUPPORT,
            "invalid_provider_structured_output_support",
        )
        context_limit = _integer(
            self.context_limit_tokens,
            minimum=1,
            maximum=_MAX_CONTEXT_TOKENS,
            reason_code="invalid_provider_context_limit",
        )
        timeout_ms = _integer(
            self.timeout_ms,
            minimum=1,
            maximum=_MAX_TIMEOUT_MS,
            reason_code="invalid_provider_timeout",
        )
        reasoning_options = _strings(
            self.reasoning_options,
            allowed=REASONING_OPTIONS,
            reason_code="invalid_provider_reasoning_options",
            require_nonempty=True,
        )
        if not isinstance(self.cost_metadata, CostMetadata):
            _fail("invalid_provider_cost_metadata")
        if not isinstance(self.enabled, bool):
            _fail("invalid_provider_enabled")
        if self.secret_reference is not None and not isinstance(
            self.secret_reference,
            SecretReference,
        ):
            _fail("invalid_provider_secret_reference")
        health_state = _enum(
            self.health_state,
            HEALTH_STATES,
            "invalid_provider_health_state",
        )
        health_reason = _enum(
            self.health_reason,
            HEALTH_REASONS,
            "invalid_provider_health_reason",
        )
        if self.enabled and health_state == "disabled":
            _fail("enabled_provider_cannot_be_disabled")
        if not self.enabled and (
            health_state != "disabled" or health_reason != "profile_disabled"
        ):
            _fail("disabled_provider_health_mismatch")
        if health_state == "healthy" and health_reason not in {"ready", "test_only"}:
            _fail("healthy_provider_reason_mismatch")
        if (
            health_reason == "credential_reference_unavailable"
            and (
                self.secret_reference is None
                or not self.secret_reference.required
                or health_state != "unavailable"
            )
        ):
            _fail("provider_credential_health_mismatch")
        if tool_support == "none" and "tool_proposals" in capability_flags:
            _fail("provider_tool_capability_mismatch")
        if tool_support != "none" and "tool_proposals" not in capability_flags:
            _fail("provider_tool_capability_mismatch")
        if structured_support == "none" and "structured_output" in capability_flags:
            _fail("provider_structured_capability_mismatch")
        if structured_support != "none" and "structured_output" not in capability_flags:
            _fail("provider_structured_capability_mismatch")
        if "reasoning" in capability_flags and reasoning_options == ("disabled",):
            _fail("provider_reasoning_capability_mismatch")
        if "local_only" in capability_flags and adapter_kind != "local_openai_compatible":
            _fail("provider_local_capability_mismatch")
        if "deterministic_fake" in capability_flags and adapter_kind != "fake":
            _fail("provider_fake_capability_mismatch")
        object.__setattr__(self, "provider_id", provider_id)
        object.__setattr__(self, "display_name", display_name)
        object.__setattr__(self, "profile_version", profile_version)
        object.__setattr__(self, "adapter_kind", adapter_kind)
        object.__setattr__(self, "adapter_version", adapter_version)
        object.__setattr__(self, "endpoint", endpoint)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "capability_flags", capability_flags)
        object.__setattr__(self, "tool_calling_support", tool_support)
        object.__setattr__(
            self,
            "structured_output_support",
            structured_support,
        )
        object.__setattr__(self, "context_limit_tokens", context_limit)
        object.__setattr__(self, "timeout_ms", timeout_ms)
        object.__setattr__(self, "reasoning_options", reasoning_options)
        object.__setattr__(self, "health_state", health_state)
        object.__setattr__(self, "health_reason", health_reason)
        expected = canonical_digest(self.identity_payload())
        if self.profile_digest:
            digest = _sha256(
                self.profile_digest,
                "invalid_provider_profile_digest",
            )
            if digest != expected:
                _fail("provider_profile_digest_mismatch")
        object.__setattr__(self, "profile_digest", expected)

    @property
    def grants_authority(self) -> bool:
        return False

    @property
    def executable(self) -> bool:
        return False

    def identity_payload(self) -> dict[str, Any]:
        return {
            "contract_version": PROVIDER_CONTRACT_VERSION,
            "provider_id": self.provider_id,
            "display_name": self.display_name,
            "profile_version": self.profile_version,
            "adapter_kind": self.adapter_kind,
            "adapter_version": self.adapter_version,
            "endpoint": self.endpoint,
            "model": self.model,
            "capability_flags": list(self.capability_flags),
            "tool_calling_support": self.tool_calling_support,
            "structured_output_support": self.structured_output_support,
            "context_limit_tokens": self.context_limit_tokens,
            "timeout_ms": self.timeout_ms,
            "reasoning_options": list(self.reasoning_options),
            "cost_metadata": self.cost_metadata.to_dict(),
            "enabled": self.enabled,
            "secret_reference": (
                None
                if self.secret_reference is None
                else self.secret_reference.to_dict()
            ),
            "health_state": self.health_state,
            "health_reason": self.health_reason,
        }

    def to_dict(self) -> dict[str, Any]:
        value = self.identity_payload()
        value["profile_digest"] = self.profile_digest
        return value

    def to_record(self) -> dict[str, Any]:
        return self.to_dict()

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ProviderProfile":
        payload = dict(
            _strict_keys(
                value,
                {
                    "contract_version",
                    "provider_id",
                    "display_name",
                    "profile_version",
                    "adapter_kind",
                    "adapter_version",
                    "endpoint",
                    "model",
                    "capability_flags",
                    "tool_calling_support",
                    "structured_output_support",
                    "context_limit_tokens",
                    "timeout_ms",
                    "reasoning_options",
                    "cost_metadata",
                    "enabled",
                    "secret_reference",
                    "health_state",
                    "health_reason",
                    "profile_digest",
                },
                "invalid_provider_profile",
            )
        )
        if payload.pop("contract_version") != PROVIDER_CONTRACT_VERSION:
            _fail("unsupported_provider_contract_version")
        if not isinstance(payload["capability_flags"], list) or not isinstance(
            payload["reasoning_options"],
            list,
        ):
            _fail("invalid_provider_profile")
        payload["capability_flags"] = tuple(payload["capability_flags"])
        payload["reasoning_options"] = tuple(payload["reasoning_options"])
        payload["cost_metadata"] = CostMetadata.from_dict(payload["cost_metadata"])
        if payload["secret_reference"] is not None:
            payload["secret_reference"] = SecretReference.from_dict(
                payload["secret_reference"]
            )
        return cls(**payload)

    @classmethod
    def from_record(cls, value: Mapping[str, Any]) -> "ProviderProfile":
        if not isinstance(value, Mapping):
            _fail("invalid_provider_profile_record")
        return cls.from_dict(value)


@dataclass(frozen=True)
class ProviderHealthAttestation:
    """Short-lived readiness evidence bound to one immutable profile."""

    provider_id: str
    profile_version: int
    profile_digest: str
    health_state: str
    checked_at: str
    expires_at: str
    evidence_digest: str
    attestation_digest: str = ""

    def __post_init__(self) -> None:
        provider_id = _identity(self.provider_id, "invalid_provider_id")
        profile_version = _integer(
            self.profile_version,
            minimum=1,
            maximum=2_147_483_647,
            reason_code="invalid_provider_profile_version",
        )
        profile_digest = _sha256(
            self.profile_digest,
            "invalid_provider_profile_digest",
        )
        health_state = _enum(
            self.health_state,
            ATTESTED_HEALTH_STATES,
            "invalid_provider_attested_health_state",
        )
        checked_at = _timestamp(
            self.checked_at,
            "invalid_provider_health_checked_at",
        )
        expires_at = _timestamp(
            self.expires_at,
            "invalid_provider_health_expires_at",
        )
        checked = _timestamp_value(checked_at)
        expires = _timestamp_value(expires_at)
        lifetime = (expires - checked).total_seconds()
        if lifetime <= 0 or lifetime > MAX_HEALTH_ATTESTATION_SECONDS:
            _fail("provider_health_attestation_lifetime_invalid")
        evidence_digest = _sha256(
            self.evidence_digest,
            "invalid_provider_health_evidence_digest",
        )
        object.__setattr__(self, "provider_id", provider_id)
        object.__setattr__(self, "profile_version", profile_version)
        object.__setattr__(self, "profile_digest", profile_digest)
        object.__setattr__(self, "health_state", health_state)
        object.__setattr__(self, "checked_at", checked_at)
        object.__setattr__(self, "expires_at", expires_at)
        object.__setattr__(self, "evidence_digest", evidence_digest)
        expected = canonical_digest(self.identity_payload())
        if self.attestation_digest:
            digest = _sha256(
                self.attestation_digest,
                "invalid_provider_health_attestation_digest",
            )
            if digest != expected:
                _fail("provider_health_attestation_digest_mismatch")
        object.__setattr__(self, "attestation_digest", expected)

    @property
    def grants_authority(self) -> bool:
        return False

    def identity_payload(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "profile_version": self.profile_version,
            "profile_digest": self.profile_digest,
            "health_state": self.health_state,
            "checked_at": self.checked_at,
            "expires_at": self.expires_at,
            "evidence_digest": self.evidence_digest,
        }

    def to_dict(self) -> dict[str, Any]:
        value = self.identity_payload()
        value["attestation_digest"] = self.attestation_digest
        return value

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "ProviderHealthAttestation":
        payload = _strict_keys(
            value,
            {
                "provider_id",
                "profile_version",
                "profile_digest",
                "health_state",
                "checked_at",
                "expires_at",
                "evidence_digest",
                "attestation_digest",
            },
            "invalid_provider_health_attestation",
        )
        return cls(**dict(payload))


@dataclass(frozen=True)
class ProviderSelection:
    provider_id: str
    profile_version: int
    profile_digest: str
    model: str
    selection_mode: str = "manual"
    fallback_used: bool = False
    grants_authority: bool = False
    health_attestation: ProviderHealthAttestation | None = None
    health_evaluated_at: str | None = None
    selection_digest: str = ""
    _factory_seal: InitVar[object | None] = None

    def __post_init__(self, _factory_seal: object | None) -> None:
        if _factory_seal is not _PROVIDER_SELECTION_FACTORY_SEAL:
            _fail("provider_selection_factory_required")
        provider_id = _identity(self.provider_id, "invalid_provider_id")
        profile_version = _integer(
            self.profile_version,
            minimum=1,
            maximum=2_147_483_647,
            reason_code="invalid_provider_profile_version",
        )
        profile_digest = _sha256(
            self.profile_digest,
            "invalid_provider_profile_digest",
        )
        model = _text(
            self.model,
            reason_code="invalid_provider_model",
            maximum=128,
        )
        if (
            self.selection_mode != "manual"
            or self.fallback_used is not False
            or self.grants_authority is not False
        ):
            _fail("provider_selection_policy_violation")
        health_attestation = self.health_attestation
        health_evaluated_at = self.health_evaluated_at
        if health_attestation is None:
            if health_evaluated_at is not None:
                _fail("provider_health_evaluation_without_attestation")
        else:
            if not isinstance(health_attestation, ProviderHealthAttestation):
                _fail("invalid_provider_health_attestation")
            if health_evaluated_at is None:
                _fail("provider_health_evaluation_time_required")
            health_evaluated_at = _timestamp(
                health_evaluated_at,
                "invalid_provider_health_evaluation_time",
            )
            if (
                health_attestation.provider_id != provider_id
                or health_attestation.profile_version != profile_version
                or health_attestation.profile_digest != profile_digest
            ):
                _fail("provider_health_attestation_binding_mismatch")
            if health_attestation.health_state == "unavailable":
                _fail("provider_health_attestation_unavailable")
        object.__setattr__(self, "provider_id", provider_id)
        object.__setattr__(self, "profile_version", profile_version)
        object.__setattr__(self, "profile_digest", profile_digest)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "health_attestation", health_attestation)
        object.__setattr__(self, "health_evaluated_at", health_evaluated_at)
        if health_attestation is not None:
            self.validate_health_at(health_evaluated_at)
        expected = canonical_digest(self.identity_payload())
        if self.selection_digest:
            digest = _sha256(
                self.selection_digest,
                "invalid_provider_selection_digest",
            )
            if digest != expected:
                _fail("provider_selection_digest_mismatch")
        object.__setattr__(self, "selection_digest", expected)

    @property
    def health_attestation_digest(self) -> str | None:
        if self.health_attestation is None:
            return None
        return self.health_attestation.attestation_digest

    def validate_health_at(self, evaluated_at: str) -> None:
        """Revalidate the full bound attestation at each execution boundary."""

        if not isinstance(self.health_attestation, ProviderHealthAttestation):
            _fail("provider_health_attestation_required")
        evaluation_time = _timestamp(
            evaluated_at,
            "invalid_provider_health_evaluation_time",
        )
        if (
            self.health_attestation.provider_id != self.provider_id
            or self.health_attestation.profile_version != self.profile_version
            or self.health_attestation.profile_digest != self.profile_digest
        ):
            _fail("provider_health_attestation_binding_mismatch")
        if self.health_attestation.health_state == "unavailable":
            _fail("provider_health_attestation_unavailable")
        evaluated = _timestamp_value(evaluation_time)
        checked = _timestamp_value(self.health_attestation.checked_at)
        expires = _timestamp_value(self.health_attestation.expires_at)
        if evaluated < checked:
            _fail("provider_health_attestation_not_yet_valid")
        if evaluated >= expires:
            _fail("provider_health_attestation_expired")

    def identity_payload(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "profile_version": self.profile_version,
            "profile_digest": self.profile_digest,
            "model": self.model,
            "selection_mode": self.selection_mode,
            "fallback_used": self.fallback_used,
            "grants_authority": self.grants_authority,
            "health_attestation": (
                None
                if self.health_attestation is None
                else self.health_attestation.to_dict()
            ),
            "health_evaluated_at": self.health_evaluated_at,
            "health_attestation_digest": self.health_attestation_digest,
        }

    def to_dict(self) -> dict[str, Any]:
        value = self.identity_payload()
        value["selection_digest"] = self.selection_digest
        return value


class ProviderRegistry:
    """Immutable, manual-only profile catalogue."""

    def __init__(
        self,
        profiles: Iterable[ProviderProfile],
        *,
        configured_secret_references: Iterable[str] = (),
    ) -> None:
        if isinstance(profiles, (str, bytes)) or not isinstance(profiles, Iterable):
            _fail("invalid_provider_registry")
        entries: dict[tuple[str, int], ProviderProfile] = {}
        current: dict[str, ProviderProfile] = {}
        for profile in profiles:
            if not isinstance(profile, ProviderProfile):
                _fail("invalid_provider_registry")
            key = (profile.provider_id, profile.profile_version)
            if key in entries:
                _fail("duplicate_provider_profile")
            entries[key] = profile
            previous = current.get(profile.provider_id)
            if previous is None or profile.profile_version > previous.profile_version:
                current[profile.provider_id] = profile
        if not entries:
            _fail("provider_registry_empty")
        references: set[str] = set()
        if (
            isinstance(configured_secret_references, (str, bytes))
            or not isinstance(configured_secret_references, Iterable)
        ):
            _fail("invalid_provider_secret_reference_set")
        for reference in configured_secret_references:
            text = _text(
                reference,
                reason_code="invalid_provider_secret_reference",
                maximum=128,
            )
            if _REFERENCE_RE.fullmatch(text) is None:
                _fail("invalid_provider_secret_reference")
            references.add(text)
        self._profiles = MappingProxyType(entries)
        self._current = MappingProxyType(current)
        self._configured_secret_references = frozenset(references)

    @property
    def grants_authority(self) -> bool:
        return False

    @property
    def executable(self) -> bool:
        return False

    def list_profiles(self) -> tuple[ProviderProfile, ...]:
        return tuple(
            self._profiles[key]
            for key in sorted(self._profiles)
        )

    def get(
        self,
        provider_id: str,
        profile_version: int | None = None,
    ) -> ProviderProfile:
        provider_id = _identity(provider_id, "invalid_provider_id")
        if profile_version is None:
            profile = self._current.get(provider_id)
        else:
            version = _integer(
                profile_version,
                minimum=1,
                maximum=2_147_483_647,
                reason_code="invalid_provider_profile_version",
            )
            profile = self._profiles.get((provider_id, version))
        if profile is None:
            _fail("provider_profile_not_found")
        return profile

    def manual_select(
        self,
        *,
        provider_id: str,
        profile_version: int,
        profile_digest: str,
        health_attestation: ProviderHealthAttestation | None = None,
        evaluated_at: str | None = None,
    ) -> ProviderSelection:
        profile = self.get(provider_id, profile_version)
        digest = _sha256(
            profile_digest,
            "invalid_provider_profile_digest",
        )
        if profile.profile_digest != digest:
            _fail("provider_profile_binding_mismatch")
        if not profile.enabled or profile.health_state == "disabled":
            _fail("provider_profile_disabled")
        if profile.health_state == "unavailable":
            _fail("provider_profile_unavailable")
        secret_reference = profile.secret_reference
        if (
            secret_reference is not None
            and secret_reference.required
            and secret_reference.reference_id
            not in self._configured_secret_references
        ):
            _fail("provider_secret_reference_unavailable")
        test_fake = (
            profile.adapter_kind == "fake"
            and profile.health_state == "healthy"
            and profile.health_reason == "test_only"
        )
        if test_fake:
            if health_attestation is not None or evaluated_at is not None:
                _fail("fake_provider_health_attestation_forbidden")
        else:
            if not isinstance(health_attestation, ProviderHealthAttestation):
                _fail("provider_health_attestation_required")
            if evaluated_at is None:
                _fail("provider_health_evaluation_time_required")
            evaluation_time = _timestamp(
                evaluated_at,
                "invalid_provider_health_evaluation_time",
            )
            if (
                health_attestation.provider_id != profile.provider_id
                or health_attestation.profile_version != profile.profile_version
                or health_attestation.profile_digest != profile.profile_digest
            ):
                _fail("provider_health_attestation_binding_mismatch")
            if health_attestation.health_state == "unavailable":
                _fail("provider_health_attestation_unavailable")
            evaluated = _timestamp_value(evaluation_time)
            checked = _timestamp_value(health_attestation.checked_at)
            expires = _timestamp_value(health_attestation.expires_at)
            if evaluated < checked:
                _fail("provider_health_attestation_not_yet_valid")
            if evaluated >= expires:
                _fail("provider_health_attestation_expired")
        return ProviderSelection(
            provider_id=profile.provider_id,
            profile_version=profile.profile_version,
            profile_digest=profile.profile_digest,
            model=profile.model,
            health_attestation=health_attestation,
            health_evaluated_at=(
                None if test_fake else evaluation_time
            ),
            _factory_seal=_PROVIDER_SELECTION_FACTORY_SEAL,
        )

    def profile_for_selection(
        self,
        selection: ProviderSelection,
    ) -> ProviderProfile:
        if not isinstance(selection, ProviderSelection):
            _fail("invalid_provider_selection")
        profile = self.get(selection.provider_id, selection.profile_version)
        if (
            selection.profile_digest != profile.profile_digest
            or selection.model != profile.model
        ):
            _fail("provider_profile_binding_mismatch")
        return profile

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "contract_version": PROVIDER_CONTRACT_VERSION,
            "selection_policy": "manual_only",
            "hidden_fallback": False,
            "grants_authority": False,
            "executable": False,
            "profiles": [profile.to_dict() for profile in self.list_profiles()],
        }


@dataclass(frozen=True)
class ProviderAdapterRequest:
    """Closed request envelope handed to a selected adapter."""

    request_id: str
    provider_id: str
    profile_version: int
    profile_digest: str
    model: str
    input_text: str
    response_mode: str
    reasoning_option: str
    max_output_tokens: int
    allow_tool_proposals: bool
    request_digest: str = ""

    def __post_init__(self) -> None:
        request_id = _identity(self.request_id, "invalid_provider_request_id")
        provider_id = _identity(self.provider_id, "invalid_provider_id")
        profile_version = _integer(
            self.profile_version,
            minimum=1,
            maximum=2_147_483_647,
            reason_code="invalid_provider_profile_version",
        )
        profile_digest = _sha256(
            self.profile_digest,
            "invalid_provider_profile_digest",
        )
        model = _text(
            self.model,
            reason_code="invalid_provider_model",
            maximum=128,
        )
        input_text = _text(
            self.input_text,
            reason_code="invalid_provider_input",
            maximum=_MAX_TEXT_CHARS,
        )
        response_mode = _enum(
            self.response_mode,
            RESPONSE_MODES,
            "invalid_provider_response_mode",
        )
        reasoning_option = _enum(
            self.reasoning_option,
            REASONING_OPTIONS,
            "invalid_provider_reasoning_option",
        )
        max_output_tokens = _integer(
            self.max_output_tokens,
            minimum=1,
            maximum=_MAX_OUTPUT_TOKENS,
            reason_code="invalid_provider_output_budget",
        )
        if not isinstance(self.allow_tool_proposals, bool):
            _fail("invalid_provider_tool_proposal_flag")
        object.__setattr__(self, "request_id", request_id)
        object.__setattr__(self, "provider_id", provider_id)
        object.__setattr__(self, "profile_version", profile_version)
        object.__setattr__(self, "profile_digest", profile_digest)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "input_text", input_text)
        object.__setattr__(self, "response_mode", response_mode)
        object.__setattr__(self, "reasoning_option", reasoning_option)
        object.__setattr__(self, "max_output_tokens", max_output_tokens)
        expected = canonical_digest(self.identity_payload())
        if self.request_digest:
            digest = _sha256(
                self.request_digest,
                "invalid_provider_request_digest",
            )
            if digest != expected:
                _fail("provider_request_digest_mismatch")
        object.__setattr__(self, "request_digest", expected)

    def identity_payload(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "provider_id": self.provider_id,
            "profile_version": self.profile_version,
            "profile_digest": self.profile_digest,
            "model": self.model,
            "input_text": self.input_text,
            "response_mode": self.response_mode,
            "reasoning_option": self.reasoning_option,
            "max_output_tokens": self.max_output_tokens,
            "allow_tool_proposals": self.allow_tool_proposals,
        }


@dataclass(frozen=True)
class ProviderUsage:
    input_tokens: int
    output_tokens: int
    total_tokens: int

    def __post_init__(self) -> None:
        input_tokens = _integer(
            self.input_tokens,
            minimum=0,
            maximum=2_147_483_647,
            reason_code="invalid_provider_usage",
        )
        output_tokens = _integer(
            self.output_tokens,
            minimum=0,
            maximum=2_147_483_647,
            reason_code="invalid_provider_usage",
        )
        total_tokens = _integer(
            self.total_tokens,
            minimum=0,
            maximum=2_147_483_647,
            reason_code="invalid_provider_usage",
        )
        if total_tokens != input_tokens + output_tokens:
            _fail("provider_usage_total_mismatch")

    def to_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass(frozen=True)
class ProviderAdapterResponse:
    request_id: str
    request_digest: str
    provider_id: str
    profile_version: int
    profile_digest: str
    model: str
    content: str
    structured_output: Mapping[str, Any] | None
    tool_proposals: tuple[Mapping[str, Any], ...]
    usage: ProviderUsage
    finish_reason: str
    response_digest: str = ""

    def __post_init__(self) -> None:
        request_id = _identity(self.request_id, "invalid_provider_request_id")
        request_digest = _sha256(
            self.request_digest,
            "invalid_provider_request_digest",
        )
        provider_id = _identity(self.provider_id, "invalid_provider_id")
        profile_version = _integer(
            self.profile_version,
            minimum=1,
            maximum=2_147_483_647,
            reason_code="invalid_provider_profile_version",
        )
        profile_digest = _sha256(
            self.profile_digest,
            "invalid_provider_profile_digest",
        )
        model = _text(
            self.model,
            reason_code="invalid_provider_model",
            maximum=128,
        )
        content = _text(
            self.content,
            reason_code="invalid_provider_response_content",
            maximum=_MAX_TEXT_CHARS,
        )
        if self.structured_output is not None and not isinstance(
            self.structured_output,
            Mapping,
        ):
            _fail("invalid_provider_structured_output")
        if (
            isinstance(self.tool_proposals, (str, bytes))
            or not isinstance(self.tool_proposals, Iterable)
        ):
            _fail("invalid_provider_tool_proposals")
        proposals = tuple(self.tool_proposals)
        if len(proposals) > _MAX_COLLECTION_ITEMS or any(
            not isinstance(item, Mapping) for item in proposals
        ):
            _fail("invalid_provider_tool_proposals")
        if not isinstance(self.usage, ProviderUsage):
            _fail("invalid_provider_usage")
        finish_reason = _text(
            self.finish_reason,
            reason_code="invalid_provider_finish_reason",
            maximum=64,
        )
        object.__setattr__(self, "request_id", request_id)
        object.__setattr__(self, "request_digest", request_digest)
        object.__setattr__(self, "provider_id", provider_id)
        object.__setattr__(self, "profile_version", profile_version)
        object.__setattr__(self, "profile_digest", profile_digest)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "content", content)
        object.__setattr__(
            self,
            "structured_output",
            None
            if self.structured_output is None
            else _freeze_json(self.structured_output),
        )
        object.__setattr__(
            self,
            "tool_proposals",
            tuple(_freeze_json(item) for item in proposals),
        )
        object.__setattr__(self, "finish_reason", finish_reason)
        expected = canonical_digest(self.identity_payload())
        if self.response_digest:
            digest = _sha256(
                self.response_digest,
                "invalid_provider_response_digest",
            )
            if digest != expected:
                _fail("provider_response_digest_mismatch")
        object.__setattr__(self, "response_digest", expected)

    def identity_payload(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "request_digest": self.request_digest,
            "provider_id": self.provider_id,
            "profile_version": self.profile_version,
            "profile_digest": self.profile_digest,
            "model": self.model,
            "content": self.content,
            "structured_output": (
                None
                if self.structured_output is None
                else _thaw_json(self.structured_output)
            ),
            "tool_proposals": [
                _thaw_json(item) for item in self.tool_proposals
            ],
            "usage": self.usage.to_dict(),
            "finish_reason": self.finish_reason,
        }


@runtime_checkable
class ProviderAdapter(Protocol):
    provider_id: str
    profile_version: int
    profile_digest: str
    model: str

    def send(self, request: ProviderAdapterRequest) -> ProviderAdapterResponse:
        """Send an already validated request through this exact profile."""


class FakeProviderAdapter:
    """Deterministic, local-only fake used for contract tests."""

    def __init__(
        self,
        profile: ProviderProfile,
        *,
        response_text: str = "fake provider response",
    ) -> None:
        if (
            not isinstance(profile, ProviderProfile)
            or profile.adapter_kind != "fake"
            or profile.provider_id != "fake"
            or profile.health_state != "healthy"
        ):
            _fail("invalid_fake_provider_profile")
        self.provider_id = profile.provider_id
        self.profile_version = profile.profile_version
        self.profile_digest = profile.profile_digest
        self.model = profile.model
        self._profile = profile
        self._response_text = _text(
            response_text,
            reason_code="invalid_fake_provider_response",
            maximum=4096,
        )

    def send(self, request: ProviderAdapterRequest) -> ProviderAdapterResponse:
        if not isinstance(request, ProviderAdapterRequest):
            _fail("invalid_provider_request")
        if (
            request.provider_id != self.provider_id
            or request.profile_version != self.profile_version
            or request.profile_digest != self.profile_digest
            or request.model != self.model
        ):
            _fail("provider_adapter_binding_mismatch")
        if request.reasoning_option not in self._profile.reasoning_options:
            _fail("provider_reasoning_option_unsupported")
        if (
            request.response_mode == "json_schema"
            and self._profile.structured_output_support != "json_schema"
        ):
            _fail("provider_response_mode_unsupported")
        if (
            request.response_mode == "json_object"
            and self._profile.structured_output_support == "none"
        ):
            _fail("provider_response_mode_unsupported")
        if (
            request.allow_tool_proposals
            and self._profile.tool_calling_support == "none"
        ):
            _fail("provider_tool_proposals_unsupported")
        structured: dict[str, Any] | None = None
        if request.response_mode != "text":
            structured = {
                "kind": "fake_contract_response",
                "request_digest": request.request_digest,
            }
        input_tokens = max(1, len(request.input_text.encode("utf-8")) // 4)
        output_tokens = max(1, len(self._response_text.encode("utf-8")) // 4)
        return ProviderAdapterResponse(
            request_id=request.request_id,
            request_digest=request.request_digest,
            provider_id=self.provider_id,
            profile_version=self.profile_version,
            profile_digest=self.profile_digest,
            model=self.model,
            content=self._response_text,
            structured_output=structured,
            tool_proposals=(),
            usage=ProviderUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
            ),
            finish_reason="stop",
        )


def _health(
    *,
    enabled: bool,
    secret_reference: SecretReference | None,
    configured_secret_references: frozenset[str],
    test_only: bool = False,
) -> tuple[str, str]:
    if not enabled:
        return "disabled", "profile_disabled"
    if test_only:
        return "healthy", "test_only"
    if (
        secret_reference is not None
        and secret_reference.required
        and secret_reference.reference_id not in configured_secret_references
    ):
        return "unavailable", "credential_reference_unavailable"
    return "unknown", "health_not_checked"


def suggested_provider_profiles(
    configured_secret_references: Iterable[str] = (),
    *,
    allow_fake: bool = False,
) -> tuple[ProviderProfile, ...]:
    """Return code-owned descriptors without resolving or reading any secret."""

    references: set[str] = set()
    if (
        isinstance(configured_secret_references, (str, bytes))
        or not isinstance(configured_secret_references, Iterable)
    ):
        _fail("invalid_provider_secret_reference_set")
    for value in configured_secret_references:
        text = _text(
            value,
            reason_code="invalid_provider_secret_reference",
            maximum=128,
        )
        if _REFERENCE_RE.fullmatch(text) is None:
            _fail("invalid_provider_secret_reference")
        references.add(text)
    configured = frozenset(references)

    deepseek_secret = SecretReference(
        "secret.deepseek.production",
        "v8_private_file",
    )
    kimi_secret = SecretReference(
        "secret.kimi.compatible",
        "local_broker",
    )
    generic_secret = SecretReference(
        "secret.openai_compatible.manual",
        "local_broker",
    )
    deepseek_health = _health(
        enabled=True,
        secret_reference=deepseek_secret,
        configured_secret_references=configured,
    )
    kimi_health = _health(
        enabled=True,
        secret_reference=kimi_secret,
        configured_secret_references=configured,
    )
    fake_health = _health(
        enabled=allow_fake,
        secret_reference=None,
        configured_secret_references=configured,
        test_only=allow_fake,
    )
    profiles = (
        ProviderProfile(
            provider_id="deepseek",
            display_name="DeepSeek Production",
            profile_version=1,
            adapter_kind="deepseek_v8_runtime",
            adapter_version="v8-runtime-1",
            endpoint="https://api.deepseek.com",
            model="deepseek-v4-flash",
            capability_flags=("chat", "usage_reporting"),
            tool_calling_support="none",
            structured_output_support="none",
            context_limit_tokens=65_536,
            timeout_ms=60_000,
            reasoning_options=("disabled",),
            cost_metadata=CostMetadata("unknown", "CNY", None, None),
            enabled=True,
            secret_reference=deepseek_secret,
            health_state=deepseek_health[0],
            health_reason=deepseek_health[1],
        ),
        ProviderProfile(
            provider_id="kimi",
            display_name="Kimi Compatible",
            profile_version=1,
            adapter_kind="openai_compatible",
            adapter_version="openai-compatible-1",
            endpoint="https://api.moonshot.cn/v1",
            model="kimi-k3",
            capability_flags=(
                "chat",
                "structured_output",
                "tool_proposals",
                "usage_reporting",
            ),
            tool_calling_support="compatible",
            structured_output_support="json_object",
            context_limit_tokens=8_192,
            timeout_ms=60_000,
            reasoning_options=("provider_default",),
            cost_metadata=CostMetadata("unknown", "CNY", None, None),
            enabled=True,
            secret_reference=kimi_secret,
            health_state=kimi_health[0],
            health_reason=kimi_health[1],
        ),
        ProviderProfile(
            provider_id="openai.compatible",
            display_name="Future OpenAI Compatible Provider",
            profile_version=1,
            adapter_kind="openai_compatible",
            adapter_version="openai-compatible-1",
            endpoint="https://provider.invalid/v1",
            model="manual-model",
            capability_flags=("chat",),
            tool_calling_support="none",
            structured_output_support="none",
            context_limit_tokens=8_192,
            timeout_ms=60_000,
            reasoning_options=("provider_default",),
            cost_metadata=CostMetadata("unknown", "USD", None, None),
            enabled=False,
            secret_reference=generic_secret,
            health_state="disabled",
            health_reason="profile_disabled",
        ),
        ProviderProfile(
            provider_id="local.openai_compatible",
            display_name="Future Local Provider",
            profile_version=1,
            adapter_kind="local_openai_compatible",
            adapter_version="openai-compatible-1",
            endpoint="http://127.0.0.1:11434/v1",
            model="manual-local-model",
            capability_flags=("chat", "local_only"),
            tool_calling_support="none",
            structured_output_support="none",
            context_limit_tokens=8_192,
            timeout_ms=60_000,
            reasoning_options=("provider_default",),
            cost_metadata=CostMetadata("not_applicable", None, None, None),
            enabled=False,
            secret_reference=None,
            health_state="disabled",
            health_reason="profile_disabled",
        ),
        ProviderProfile(
            provider_id="fake",
            display_name="Deterministic Fake Provider",
            profile_version=1,
            adapter_kind="fake",
            adapter_version="fake-contract-1",
            endpoint="fake://contract/v1",
            model="fake-v1",
            capability_flags=(
                "chat",
                "deterministic_fake",
                "structured_output",
                "tool_proposals",
                "usage_reporting",
            ),
            tool_calling_support="compatible",
            structured_output_support="json_schema",
            context_limit_tokens=4_096,
            timeout_ms=1_000,
            reasoning_options=("disabled",),
            cost_metadata=CostMetadata("not_applicable", None, None, None),
            enabled=allow_fake,
            secret_reference=None,
            health_state=fake_health[0],
            health_reason=fake_health[1],
        ),
    )
    return tuple(sorted(profiles, key=lambda item: item.provider_id))


def build_suggested_provider_registry(
    configured_secret_references: Iterable[str] = (),
    *,
    allow_fake: bool = False,
) -> ProviderRegistry:
    references = tuple(configured_secret_references)
    return ProviderRegistry(
        suggested_provider_profiles(references, allow_fake=allow_fake),
        configured_secret_references=references,
    )


__all__ = [
    "ADAPTER_KINDS",
    "ATTESTED_HEALTH_STATES",
    "CAPABILITY_FLAGS",
    "HEALTH_REASONS",
    "HEALTH_STATES",
    "MAX_HEALTH_ATTESTATION_SECONDS",
    "PROVIDER_CONTRACT_VERSION",
    "PRICING_STATES",
    "REASONING_OPTIONS",
    "RESOLVER_KINDS",
    "RESPONSE_MODES",
    "STRUCTURED_OUTPUT_SUPPORT",
    "TOOL_CALLING_SUPPORT",
    "CognitiveProviderError",
    "CostMetadata",
    "FakeProviderAdapter",
    "ProviderAdapter",
    "ProviderAdapterRequest",
    "ProviderAdapterResponse",
    "ProviderHealthAttestation",
    "ProviderProfile",
    "ProviderRegistry",
    "ProviderSelection",
    "ProviderUsage",
    "SecretReference",
    "build_suggested_provider_registry",
    "suggested_provider_profiles",
]
