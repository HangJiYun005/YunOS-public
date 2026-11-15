"""One-shot external model-send consent bound to immutable v10-C contracts.

The module is pure: it performs no file, environment, credential, database, or
network I/O.  A consent cannot be reconstructed from a boolean flag and never
grants project write, delegation, acceptance, retry, or Provider fallback.
"""
from __future__ import annotations

from dataclasses import InitVar, dataclass
from datetime import datetime
import hashlib
from typing import Any, Mapping

from memory.cognitive_orchestration import (
    CognitiveModelRequest,
    ModelCallReservation,
    contains_secret_material,
)
from memory.cognitive_provider import (
    CognitiveProviderError,
    ProviderSelection,
)
from memory.operational_state_models import (
    OperationalStateError,
    canonical_digest,
    normalize_timestamp,
    validate_identity,
    validate_sha256,
)


EXTERNAL_SEND_CONSENT_VERSION = "v10-c-external-send-consent-1"
TRUSTED_USER_EVENT_VERSION = "v10-f-trusted-user-event-1"
TRUSTED_EXTERNAL_SEND_CONSENT_VERSION = "v10-f-trusted-external-send-consent-1"
TRUSTED_LOCAL_CONTROLLER_VERSION = "v10-f-trusted-local-controller-1"
TRUSTED_LOCAL_CONTROLLER_ID = "controller:yunos-v10-f-real-agent-cli"
V10_F_USER_AUTHORIZATION_UTTERANCE = (
    "我知情并批准上述 DeepSeek 外发边界。"
)
V10_F_USER_AUTHORIZATION_TEXT_SHA256 = hashlib.sha256(
    V10_F_USER_AUTHORIZATION_UTTERANCE.encode("utf-8")
).hexdigest()
MAX_EXTERNAL_SEND_CONSENT_SECONDS = 1_800
_CONSENT_FACTORY_SEAL = object()
_TRUSTED_USER_EVENT_FACTORY_SEAL = object()
_TRUSTED_LOCAL_CONTROLLER_FACTORY_SEAL = object()


class ExternalSendConsentError(ValueError):
    """Sanitized, fail-closed consent contract error."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def _fail(reason_code: str) -> None:
    raise ExternalSendConsentError(reason_code)


def _identity(value: Any, reason_code: str) -> str:
    try:
        return validate_identity(value, reason_code)
    except OperationalStateError:
        _fail(reason_code)


def _digest(value: Any, reason_code: str) -> str:
    try:
        return validate_sha256(value, reason_code)
    except OperationalStateError:
        _fail(reason_code)


def _timestamp(value: Any, reason_code: str) -> str:
    try:
        return normalize_timestamp(value)
    except OperationalStateError:
        _fail(reason_code)


def _timestamp_value(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _text(value: Any, reason_code: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        _fail(reason_code)
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > maximum
        or "\x00" in normalized
        or any(ord(character) < 32 for character in normalized)
    ):
        _fail(reason_code)
    return normalized


def reservation_digest(reservation: ModelCallReservation) -> str:
    """Canonical digest shared by consent and Runtime preview validation."""

    if not isinstance(reservation, ModelCallReservation):
        _fail("typed_model_call_reservation_required")
    return canonical_digest(
        {
            "reservation_id": reservation.reservation_id,
            "request_digest": reservation.request_digest,
            "provider_binding_digest": (
                reservation.provider_binding.binding_digest
            ),
            "input_tokens": reservation.input_tokens,
            "output_token_ceiling": reservation.output_token_ceiling,
            "cost_microunits_ceiling": (
                reservation.cost_microunits_ceiling
            ),
            "sequence": reservation.sequence,
            "grants_authority": reservation.grants_authority,
            "execution_allowed": reservation.execution_allowed,
        }
    )


@dataclass(frozen=True)
class TrustedLocalControllerAuthority:
    """One short-lived local CLI issuer for the two v10-F Agent turns."""

    controller_id: str
    user_authorization_text_sha256: str
    cli_confirmation_digest: str
    issued_at: str
    expires_at: str
    maximum_events: int = 2
    exact_cli_confirmed: bool = True
    two_model_calls_confirmed: bool = True
    do_not_rerun_confirmed: bool = True
    authority_digest: str = ""
    _factory_seal: InitVar[object | None] = None

    def __post_init__(self, _factory_seal: object | None) -> None:
        if _factory_seal is not _TRUSTED_LOCAL_CONTROLLER_FACTORY_SEAL:
            _fail("trusted_local_controller_factory_required")
        controller_id = _identity(
            self.controller_id,
            "invalid_trusted_local_controller_id",
        )
        if controller_id != TRUSTED_LOCAL_CONTROLLER_ID:
            _fail("trusted_local_controller_id_mismatch")
        object.__setattr__(self, "controller_id", controller_id)
        authorization_digest = _digest(
            self.user_authorization_text_sha256,
            "invalid_trusted_local_controller_user_authorization_digest",
        )
        if authorization_digest != V10_F_USER_AUTHORIZATION_TEXT_SHA256:
            _fail("trusted_local_controller_user_authorization_mismatch")
        object.__setattr__(
            self,
            "user_authorization_text_sha256",
            authorization_digest,
        )
        object.__setattr__(
            self,
            "cli_confirmation_digest",
            _digest(
                self.cli_confirmation_digest,
                "invalid_trusted_local_controller_cli_confirmation_digest",
            ),
        )
        issued = _timestamp(
            self.issued_at,
            "invalid_trusted_local_controller_issued_at",
        )
        expires = _timestamp(
            self.expires_at,
            "invalid_trusted_local_controller_expires_at",
        )
        lifetime = (_timestamp_value(expires) - _timestamp_value(issued)).total_seconds()
        if lifetime <= 0 or lifetime > MAX_EXTERNAL_SEND_CONSENT_SECONDS:
            _fail("trusted_local_controller_lifetime_invalid")
        object.__setattr__(self, "issued_at", issued)
        object.__setattr__(self, "expires_at", expires)
        if (
            type(self.maximum_events) is not int
            or self.maximum_events != 2
            or self.exact_cli_confirmed is not True
            or self.two_model_calls_confirmed is not True
            or self.do_not_rerun_confirmed is not True
        ):
            _fail("trusted_local_controller_scope_invalid")
        expected = canonical_digest(self.identity_payload())
        if self.authority_digest:
            supplied = _digest(
                self.authority_digest,
                "invalid_trusted_local_controller_authority_digest",
            )
            if supplied != expected:
                _fail("trusted_local_controller_authority_digest_mismatch")
        object.__setattr__(self, "authority_digest", expected)

    def identity_payload(self) -> dict[str, Any]:
        return {
            "contract_version": TRUSTED_LOCAL_CONTROLLER_VERSION,
            "controller_id": self.controller_id,
            "user_authorization_text_sha256": (
                self.user_authorization_text_sha256
            ),
            "cli_confirmation_digest": self.cli_confirmation_digest,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "maximum_events": self.maximum_events,
            "exact_cli_confirmed": self.exact_cli_confirmed,
            "two_model_calls_confirmed": self.two_model_calls_confirmed,
            "do_not_rerun_confirmed": self.do_not_rerun_confirmed,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_payload(), "authority_digest": self.authority_digest}


def _validate_trusted_local_controller(
    value: Any,
    *,
    evaluated_at: str,
) -> TrustedLocalControllerAuthority:
    if type(value) is not TrustedLocalControllerAuthority:
        _fail("exact_trusted_local_controller_required")
    rebuilt = TrustedLocalControllerAuthority(
        controller_id=value.controller_id,
        user_authorization_text_sha256=value.user_authorization_text_sha256,
        cli_confirmation_digest=value.cli_confirmation_digest,
        issued_at=value.issued_at,
        expires_at=value.expires_at,
        maximum_events=value.maximum_events,
        exact_cli_confirmed=value.exact_cli_confirmed,
        two_model_calls_confirmed=value.two_model_calls_confirmed,
        do_not_rerun_confirmed=value.do_not_rerun_confirmed,
        authority_digest=value.authority_digest,
        _factory_seal=_TRUSTED_LOCAL_CONTROLLER_FACTORY_SEAL,
    )
    if value.to_dict() != rebuilt.to_dict():
        _fail("trusted_local_controller_integrity_mismatch")
    evaluated = _timestamp(evaluated_at, "invalid_trusted_local_controller_evaluation")
    if _timestamp_value(evaluated) < _timestamp_value(rebuilt.issued_at):
        _fail("trusted_local_controller_not_yet_valid")
    if _timestamp_value(evaluated) >= _timestamp_value(rebuilt.expires_at):
        _fail("trusted_local_controller_expired")
    return rebuilt


def open_v10_f_trusted_local_controller(
    *,
    user_authorization_text_sha256: str,
    cli_confirmation_digest: str,
    issued_at: str,
    expires_at: str,
    confirm_exact_cli: bool,
    confirm_two_model_calls: bool,
    confirm_do_not_rerun: bool,
) -> TrustedLocalControllerAuthority:
    """Open the fixed local CLI issuer; accepts no caller-defined scope."""

    if (
        confirm_exact_cli is not True
        or confirm_two_model_calls is not True
        or confirm_do_not_rerun is not True
    ):
        _fail("trusted_local_controller_confirmation_required")
    return TrustedLocalControllerAuthority(
        controller_id=TRUSTED_LOCAL_CONTROLLER_ID,
        user_authorization_text_sha256=user_authorization_text_sha256,
        cli_confirmation_digest=cli_confirmation_digest,
        issued_at=issued_at,
        expires_at=expires_at,
        _factory_seal=_TRUSTED_LOCAL_CONTROLLER_FACTORY_SEAL,
    )


@dataclass(frozen=True)
class TrustedUserEventArtifact:
    """Factory-sealed observation of one local operator confirmation.

    ``observed_at`` is the release controller's observation time.  It never
    pretends to be the timestamp of an earlier chat message.  The artifact is
    disclosure-specific and therefore cannot authorize a later model send.
    """

    event_id: str
    user_utterance: str
    runtime_disclosure_digest: str
    confirmation_binding_digest: str
    controller_authority_digest: str
    intended_consent_id: str
    sequence: int
    observed_at: str
    evidence_source: str = "trusted_local_operator_cli_confirmation"
    trusted_user_event_verified: bool = True
    user_utterance_sha256: str = ""
    event_digest: str = ""
    _factory_seal: InitVar[object | None] = None

    def __post_init__(self, _factory_seal: object | None) -> None:
        if _factory_seal is not _TRUSTED_USER_EVENT_FACTORY_SEAL:
            _fail("trusted_user_event_factory_required")
        object.__setattr__(
            self,
            "event_id",
            _identity(self.event_id, "invalid_trusted_user_event_id"),
        )
        utterance = _text(
            self.user_utterance,
            "invalid_trusted_user_event_utterance",
            maximum=512,
        )
        if contains_secret_material({"user_utterance": utterance}):
            _fail("trusted_user_event_secret_forbidden")
        object.__setattr__(self, "user_utterance", utterance)
        for name in (
            "runtime_disclosure_digest",
            "confirmation_binding_digest",
            "controller_authority_digest",
        ):
            object.__setattr__(
                self,
                name,
                _digest(getattr(self, name), f"invalid_trusted_user_event_{name}"),
            )
        observed = _timestamp(
            self.observed_at,
            "invalid_trusted_user_event_observed_at",
        )
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(
            self,
            "intended_consent_id",
            _identity(
                self.intended_consent_id,
                "invalid_trusted_user_event_intended_consent_id",
            ),
        )
        if (
            self.evidence_source
            != "trusted_local_operator_cli_confirmation"
            or self.trusted_user_event_verified is not True
            or type(self.sequence) is not int
            or self.sequence not in {1, 2}
        ):
            _fail("trusted_user_event_source_invalid")
        utterance_digest = hashlib.sha256(utterance.encode("utf-8")).hexdigest()
        if self.user_utterance_sha256:
            supplied = _digest(
                self.user_utterance_sha256,
                "invalid_trusted_user_event_utterance_digest",
            )
            if supplied != utterance_digest:
                _fail("trusted_user_event_utterance_digest_mismatch")
        object.__setattr__(self, "user_utterance_sha256", utterance_digest)
        expected = canonical_digest(self.identity_payload())
        if self.event_digest:
            supplied_event = _digest(
                self.event_digest,
                "invalid_trusted_user_event_digest",
            )
            if supplied_event != expected:
                _fail("trusted_user_event_digest_mismatch")
        object.__setattr__(self, "event_digest", expected)

    def identity_payload(self) -> dict[str, Any]:
        return {
            "contract_version": TRUSTED_USER_EVENT_VERSION,
            "event_id": self.event_id,
            "user_utterance": self.user_utterance,
            "user_utterance_sha256": self.user_utterance_sha256,
            "runtime_disclosure_digest": self.runtime_disclosure_digest,
            "confirmation_binding_digest": self.confirmation_binding_digest,
            "controller_authority_digest": self.controller_authority_digest,
            "intended_consent_id": self.intended_consent_id,
            "sequence": self.sequence,
            "observed_at": self.observed_at,
            "evidence_source": self.evidence_source,
            "trusted_user_event_verified": self.trusted_user_event_verified,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_payload(), "event_digest": self.event_digest}


def create_trusted_user_event(
    *,
    controller: TrustedLocalControllerAuthority,
    event_id: str,
    user_utterance: str,
    runtime_disclosure_digest: str,
    confirmation_binding_digest: str,
    intended_consent_id: str,
    sequence: int,
    observed_at: str,
) -> TrustedUserEventArtifact:
    """Create one disclosure-bound trusted event at the controller boundary."""

    validated_controller = _validate_trusted_local_controller(
        controller,
        evaluated_at=observed_at,
    )
    normalized_utterance = _text(
        user_utterance,
        "invalid_trusted_user_event_utterance",
        maximum=512,
    )
    if (
        hashlib.sha256(normalized_utterance.encode("utf-8")).hexdigest()
        != validated_controller.user_authorization_text_sha256
        or confirmation_binding_digest
        != validated_controller.cli_confirmation_digest
    ):
        _fail("trusted_user_event_controller_binding_mismatch")
    return TrustedUserEventArtifact(
        event_id=event_id,
        user_utterance=normalized_utterance,
        runtime_disclosure_digest=runtime_disclosure_digest,
        confirmation_binding_digest=confirmation_binding_digest,
        controller_authority_digest=validated_controller.authority_digest,
        intended_consent_id=intended_consent_id,
        sequence=sequence,
        observed_at=observed_at,
        _factory_seal=_TRUSTED_USER_EVENT_FACTORY_SEAL,
    )


def _validate_trusted_user_event_artifact(
    value: Any,
) -> TrustedUserEventArtifact:
    if type(value) is not TrustedUserEventArtifact:
        _fail("exact_trusted_user_event_required")
    rebuilt = TrustedUserEventArtifact(
        event_id=value.event_id,
        user_utterance=value.user_utterance,
        runtime_disclosure_digest=value.runtime_disclosure_digest,
        confirmation_binding_digest=value.confirmation_binding_digest,
        controller_authority_digest=value.controller_authority_digest,
        intended_consent_id=value.intended_consent_id,
        sequence=value.sequence,
        observed_at=value.observed_at,
        evidence_source=value.evidence_source,
        trusted_user_event_verified=value.trusted_user_event_verified,
        user_utterance_sha256=value.user_utterance_sha256,
        event_digest=value.event_digest,
        _factory_seal=_TRUSTED_USER_EVENT_FACTORY_SEAL,
    )
    if value.to_dict() != rebuilt.to_dict():
        _fail("trusted_user_event_integrity_mismatch")
    return rebuilt


@dataclass(frozen=True)
class ExternalSendConsent:
    consent_id: str
    user_utterance: str
    disclosure_digest: str
    objective_id: str
    objective_digest: str
    context_digest: str
    provider_selection_digest: str
    health_attestation_digest: str
    request_digest: str
    reservation_id: str
    reservation_digest: str
    runtime_disclosure_digest: str
    runtime_disclosure_prepared_at: str
    runtime_disclosure_expires_at: str
    runtime_request_id: str
    runtime_mutation_id: str
    runtime_budget_profile: str
    runtime_question_sha256: str
    runtime_reviewed_binding_digest: str
    runtime_artifact_identity: str
    runtime_model_run_id: str
    provider_id: str
    model: str
    purpose: str
    granted_at: str
    expires_at: str
    evidence_source: str = "caller_supplied_interactive_claim"
    trusted_user_event_verified: bool = False
    trusted_user_event_digest: str | None = None
    maximum_model_calls: int = 1
    automatic_retry_allowed: bool = False
    provider_fallback_allowed: bool = False
    project_write_allowed: bool = False
    delegation_allowed: bool = False
    state_update_allowed: bool = False
    result_acceptance_allowed: bool = False
    consent_digest: str = ""
    _factory_seal: InitVar[object | None] = None

    def __post_init__(self, _factory_seal: object | None) -> None:
        if _factory_seal is not _CONSENT_FACTORY_SEAL:
            _fail("external_send_consent_factory_required")
        object.__setattr__(
            self,
            "consent_id",
            _identity(self.consent_id, "invalid_external_send_consent_id"),
        )
        user_utterance = _text(
            self.user_utterance,
            "invalid_external_send_user_utterance",
            maximum=512,
        )
        if contains_secret_material({"user_utterance": user_utterance}):
            _fail("external_send_consent_secret_forbidden")
        object.__setattr__(self, "user_utterance", user_utterance)
        for name in (
            "disclosure_digest",
            "objective_digest",
            "context_digest",
            "provider_selection_digest",
            "health_attestation_digest",
            "request_digest",
            "reservation_digest",
            "runtime_disclosure_digest",
            "runtime_question_sha256",
            "runtime_reviewed_binding_digest",
        ):
            object.__setattr__(
                self,
                name,
                _digest(
                    getattr(self, name),
                    f"invalid_external_send_{name}",
                ),
            )
        for name in (
            "objective_id",
            "reservation_id",
            "runtime_request_id",
            "runtime_mutation_id",
            "runtime_artifact_identity",
            "runtime_model_run_id",
            "provider_id",
        ):
            object.__setattr__(
                self,
                name,
                _identity(
                    getattr(self, name),
                    f"invalid_external_send_{name}",
                ),
            )
        object.__setattr__(
            self,
            "model",
            _text(self.model, "invalid_external_send_model", maximum=128),
        )
        if self.runtime_budget_profile not in {"Small", "Medium"}:
            _fail("external_send_runtime_budget_profile_forbidden")
        if self.purpose not in {"discussion", "bounded_tool_proposal"}:
            _fail("external_send_purpose_forbidden")
        if (
            self.runtime_budget_profile == "Medium"
            and self.purpose != "bounded_tool_proposal"
        ):
            _fail("external_send_runtime_budget_profile_forbidden")
        if self.evidence_source == "caller_supplied_interactive_claim":
            if (
                self.trusted_user_event_verified is not False
                or self.trusted_user_event_digest is not None
            ):
                _fail("trusted_user_event_verification_unavailable")
        elif self.evidence_source == "trusted_local_operator_cli_confirmation":
            if self.trusted_user_event_verified is not True:
                _fail("trusted_user_event_verification_required")
            object.__setattr__(
                self,
                "trusted_user_event_digest",
                _digest(
                    self.trusted_user_event_digest,
                    "invalid_trusted_user_event_digest",
                ),
            )
        else:
            _fail("external_send_evidence_source_forbidden")
        granted_at = _timestamp(
            self.granted_at,
            "invalid_external_send_granted_at",
        )
        expires_at = _timestamp(
            self.expires_at,
            "invalid_external_send_expires_at",
        )
        lifetime = (
            _timestamp_value(expires_at) - _timestamp_value(granted_at)
        ).total_seconds()
        if lifetime <= 0 or lifetime > MAX_EXTERNAL_SEND_CONSENT_SECONDS:
            _fail("external_send_consent_lifetime_invalid")
        disclosure_expires_at = _timestamp(
            self.runtime_disclosure_expires_at,
            "invalid_external_send_runtime_disclosure_expires_at",
        )
        disclosure_prepared_at = _timestamp(
            self.runtime_disclosure_prepared_at,
            "invalid_external_send_runtime_disclosure_prepared_at",
        )
        if _timestamp_value(granted_at) < _timestamp_value(
            disclosure_prepared_at
        ):
            _fail("external_send_consent_precedes_disclosure")
        if _timestamp_value(expires_at) > _timestamp_value(
            disclosure_expires_at
        ):
            _fail("external_send_consent_exceeds_disclosure_lifetime")
        object.__setattr__(self, "granted_at", granted_at)
        object.__setattr__(self, "expires_at", expires_at)
        object.__setattr__(
            self,
            "runtime_disclosure_prepared_at",
            disclosure_prepared_at,
        )
        object.__setattr__(
            self,
            "runtime_disclosure_expires_at",
            disclosure_expires_at,
        )
        if (
            isinstance(self.maximum_model_calls, bool)
            or self.maximum_model_calls != 1
            or self.automatic_retry_allowed is not False
            or self.provider_fallback_allowed is not False
            or self.project_write_allowed is not False
            or self.delegation_allowed is not False
            or self.state_update_allowed is not False
            or self.result_acceptance_allowed is not False
        ):
            _fail("external_send_consent_scope_violation")
        expected = canonical_digest(self.identity_payload())
        if self.consent_digest:
            supplied = _digest(
                self.consent_digest,
                "invalid_external_send_consent_digest",
            )
            if supplied != expected:
                _fail("external_send_consent_digest_mismatch")
        object.__setattr__(self, "consent_digest", expected)

    @property
    def grants_authority(self) -> bool:
        return False

    def identity_payload(self) -> dict[str, Any]:
        payload = {
            "contract_version": (
                TRUSTED_EXTERNAL_SEND_CONSENT_VERSION
                if self.trusted_user_event_verified is True
                else EXTERNAL_SEND_CONSENT_VERSION
            ),
            "consent_id": self.consent_id,
            "user_utterance": self.user_utterance,
            "disclosure_digest": self.disclosure_digest,
            "objective_id": self.objective_id,
            "objective_digest": self.objective_digest,
            "context_digest": self.context_digest,
            "provider_selection_digest": self.provider_selection_digest,
            "health_attestation_digest": self.health_attestation_digest,
            "request_digest": self.request_digest,
            "reservation_id": self.reservation_id,
            "reservation_digest": self.reservation_digest,
            "runtime_disclosure_digest": self.runtime_disclosure_digest,
            "runtime_disclosure_prepared_at": (
                self.runtime_disclosure_prepared_at
            ),
            "runtime_disclosure_expires_at": (
                self.runtime_disclosure_expires_at
            ),
            "runtime_request_id": self.runtime_request_id,
            "runtime_mutation_id": self.runtime_mutation_id,
            "runtime_budget_profile": self.runtime_budget_profile,
            "runtime_question_sha256": self.runtime_question_sha256,
            "runtime_reviewed_binding_digest": (
                self.runtime_reviewed_binding_digest
            ),
            "runtime_artifact_identity": self.runtime_artifact_identity,
            "runtime_model_run_id": self.runtime_model_run_id,
            "provider_id": self.provider_id,
            "model": self.model,
            "purpose": self.purpose,
            "granted_at": self.granted_at,
            "expires_at": self.expires_at,
            "evidence_source": self.evidence_source,
            "trusted_user_event_verified": self.trusted_user_event_verified,
            "maximum_model_calls": self.maximum_model_calls,
            "automatic_retry_allowed": self.automatic_retry_allowed,
            "provider_fallback_allowed": self.provider_fallback_allowed,
            "project_write_allowed": self.project_write_allowed,
            "delegation_allowed": self.delegation_allowed,
            "state_update_allowed": self.state_update_allowed,
            "result_acceptance_allowed": self.result_acceptance_allowed,
            "grants_authority": self.grants_authority,
        }
        if self.trusted_user_event_verified is True:
            payload["trusted_user_event_digest"] = self.trusted_user_event_digest
        return payload

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_payload(), "consent_digest": self.consent_digest}

    def validate_binding(
        self,
        selection: ProviderSelection,
        request: CognitiveModelRequest,
        reservation: ModelCallReservation,
        *,
        runtime_disclosure_digest: str,
        runtime_disclosure_prepared_at: str,
        runtime_disclosure_expires_at: str,
        runtime_request_id: str,
        runtime_mutation_id: str,
        runtime_budget_profile: str,
        runtime_question_sha256: str,
        runtime_reviewed_binding_digest: str,
        runtime_artifact_identity: str,
        runtime_model_run_id: str,
        evaluated_at: str,
    ) -> None:
        if not isinstance(selection, ProviderSelection):
            _fail("typed_provider_selection_required")
        if not isinstance(request, CognitiveModelRequest):
            _fail("typed_cognitive_model_request_required")
        if not isinstance(reservation, ModelCallReservation):
            _fail("typed_model_call_reservation_required")
        evaluation = _timestamp(
            evaluated_at,
            "invalid_external_send_evaluation_time",
        )
        if _timestamp_value(evaluation) < _timestamp_value(self.granted_at):
            _fail("external_send_consent_not_yet_valid")
        if _timestamp_value(evaluation) >= _timestamp_value(self.expires_at):
            _fail("external_send_consent_expired")
        try:
            selection.validate_health_at(evaluation)
        except CognitiveProviderError as exc:
            _fail(exc.reason_code)
        objective = dict(request.objective)
        expected_purpose = (
            "bounded_tool_proposal"
            if request.expected_output_kind == "tool_proposal"
            else "discussion"
        )
        if (
            self.objective_id != objective.get("objective_id")
            or self.objective_digest != objective.get("objective_digest")
            or self.context_digest != request.context.context_digest
            or self.provider_selection_digest != selection.selection_digest
            or self.health_attestation_digest
            != selection.health_attestation_digest
            or self.request_digest != request.request_digest
            or self.reservation_id != reservation.reservation_id
            or self.reservation_digest != reservation_digest(reservation)
            or self.runtime_disclosure_digest != runtime_disclosure_digest
            or self.runtime_disclosure_prepared_at
            != runtime_disclosure_prepared_at
            or self.runtime_disclosure_expires_at
            != runtime_disclosure_expires_at
            or self.runtime_request_id != runtime_request_id
            or self.runtime_mutation_id != runtime_mutation_id
            or self.runtime_budget_profile != runtime_budget_profile
            or self.runtime_question_sha256 != runtime_question_sha256
            or self.runtime_reviewed_binding_digest
            != runtime_reviewed_binding_digest
            or self.runtime_artifact_identity != runtime_artifact_identity
            or self.runtime_model_run_id != runtime_model_run_id
            or self.provider_id != selection.provider_id
            or self.model != selection.model
            or self.purpose != expected_purpose
            or request.provider_binding.selection != selection
            or reservation.provider_binding != request.provider_binding
            or reservation.request_digest != request.request_digest
        ):
            _fail("external_send_consent_binding_mismatch")


def create_external_send_consent(
    *,
    consent_id: str,
    user_utterance: str,
    disclosure_digest: str,
    selection: ProviderSelection,
    request: CognitiveModelRequest,
    reservation: ModelCallReservation,
    runtime_disclosure_digest: str,
    runtime_disclosure_prepared_at: str,
    runtime_disclosure_expires_at: str,
    runtime_request_id: str,
    runtime_mutation_id: str,
    runtime_budget_profile: str,
    runtime_question_sha256: str,
    runtime_reviewed_binding_digest: str,
    runtime_artifact_identity: str,
    runtime_model_run_id: str,
    granted_at: str,
    expires_at: str,
) -> ExternalSendConsent:
    """Bind a caller-supplied claim to exact, already reviewed contracts.

    v10-C does not authenticate a UI/session user-event issuer.  A trusted
    user-event artifact remains required before this contract becomes a
    production authorization source in v10-E/F.
    """

    if not isinstance(selection, ProviderSelection):
        _fail("typed_provider_selection_required")
    if not isinstance(request, CognitiveModelRequest):
        _fail("typed_cognitive_model_request_required")
    if not isinstance(reservation, ModelCallReservation):
        _fail("typed_model_call_reservation_required")
    objective: Mapping[str, Any] = request.objective
    consent = ExternalSendConsent(
        consent_id=consent_id,
        user_utterance=user_utterance,
        disclosure_digest=disclosure_digest,
        objective_id=objective.get("objective_id"),
        objective_digest=objective.get("objective_digest"),
        context_digest=request.context.context_digest,
        provider_selection_digest=selection.selection_digest,
        health_attestation_digest=selection.health_attestation_digest,
        request_digest=request.request_digest,
        reservation_id=reservation.reservation_id,
        reservation_digest=reservation_digest(reservation),
        runtime_disclosure_digest=runtime_disclosure_digest,
        runtime_disclosure_prepared_at=runtime_disclosure_prepared_at,
        runtime_disclosure_expires_at=runtime_disclosure_expires_at,
        runtime_request_id=runtime_request_id,
        runtime_mutation_id=runtime_mutation_id,
        runtime_budget_profile=runtime_budget_profile,
        runtime_question_sha256=runtime_question_sha256,
        runtime_reviewed_binding_digest=runtime_reviewed_binding_digest,
        runtime_artifact_identity=runtime_artifact_identity,
        runtime_model_run_id=runtime_model_run_id,
        provider_id=selection.provider_id,
        model=selection.model,
        purpose=(
            "bounded_tool_proposal"
            if request.expected_output_kind == "tool_proposal"
            else "discussion"
        ),
        granted_at=granted_at,
        expires_at=expires_at,
        _factory_seal=_CONSENT_FACTORY_SEAL,
    )
    consent.validate_binding(
        selection,
        request,
        reservation,
        runtime_disclosure_digest=runtime_disclosure_digest,
        runtime_disclosure_prepared_at=runtime_disclosure_prepared_at,
        runtime_disclosure_expires_at=runtime_disclosure_expires_at,
        runtime_request_id=runtime_request_id,
        runtime_mutation_id=runtime_mutation_id,
        runtime_budget_profile=runtime_budget_profile,
        runtime_question_sha256=runtime_question_sha256,
        runtime_reviewed_binding_digest=runtime_reviewed_binding_digest,
        runtime_artifact_identity=runtime_artifact_identity,
        runtime_model_run_id=runtime_model_run_id,
        evaluated_at=granted_at,
    )
    return consent


def create_trusted_external_send_consent(
    *,
    consent_id: str,
    trusted_user_event: TrustedUserEventArtifact,
    disclosure_digest: str,
    selection: ProviderSelection,
    request: CognitiveModelRequest,
    reservation: ModelCallReservation,
    runtime_disclosure_digest: str,
    runtime_disclosure_prepared_at: str,
    runtime_disclosure_expires_at: str,
    runtime_request_id: str,
    runtime_mutation_id: str,
    runtime_budget_profile: str,
    runtime_question_sha256: str,
    runtime_reviewed_binding_digest: str,
    runtime_artifact_identity: str,
    runtime_model_run_id: str,
    expires_at: str,
) -> ExternalSendConsent:
    """Bind one factory-sealed trusted event to one reviewed model send."""

    trusted_user_event = _validate_trusted_user_event_artifact(
        trusted_user_event
    )
    if trusted_user_event.runtime_disclosure_digest != runtime_disclosure_digest:
        _fail("trusted_user_event_disclosure_mismatch")
    if trusted_user_event.intended_consent_id != consent_id:
        _fail("trusted_user_event_consent_id_mismatch")
    if not isinstance(selection, ProviderSelection):
        _fail("typed_provider_selection_required")
    if not isinstance(request, CognitiveModelRequest):
        _fail("typed_cognitive_model_request_required")
    if not isinstance(reservation, ModelCallReservation):
        _fail("typed_model_call_reservation_required")
    objective: Mapping[str, Any] = request.objective
    consent = ExternalSendConsent(
        consent_id=consent_id,
        user_utterance=trusted_user_event.user_utterance,
        disclosure_digest=disclosure_digest,
        objective_id=objective.get("objective_id"),
        objective_digest=objective.get("objective_digest"),
        context_digest=request.context.context_digest,
        provider_selection_digest=selection.selection_digest,
        health_attestation_digest=selection.health_attestation_digest,
        request_digest=request.request_digest,
        reservation_id=reservation.reservation_id,
        reservation_digest=reservation_digest(reservation),
        runtime_disclosure_digest=runtime_disclosure_digest,
        runtime_disclosure_prepared_at=runtime_disclosure_prepared_at,
        runtime_disclosure_expires_at=runtime_disclosure_expires_at,
        runtime_request_id=runtime_request_id,
        runtime_mutation_id=runtime_mutation_id,
        runtime_budget_profile=runtime_budget_profile,
        runtime_question_sha256=runtime_question_sha256,
        runtime_reviewed_binding_digest=runtime_reviewed_binding_digest,
        runtime_artifact_identity=runtime_artifact_identity,
        runtime_model_run_id=runtime_model_run_id,
        provider_id=selection.provider_id,
        model=selection.model,
        purpose=(
            "bounded_tool_proposal"
            if request.expected_output_kind == "tool_proposal"
            else "discussion"
        ),
        granted_at=trusted_user_event.observed_at,
        expires_at=expires_at,
        evidence_source=trusted_user_event.evidence_source,
        trusted_user_event_verified=True,
        trusted_user_event_digest=trusted_user_event.event_digest,
        _factory_seal=_CONSENT_FACTORY_SEAL,
    )
    consent.validate_binding(
        selection,
        request,
        reservation,
        runtime_disclosure_digest=runtime_disclosure_digest,
        runtime_disclosure_prepared_at=runtime_disclosure_prepared_at,
        runtime_disclosure_expires_at=runtime_disclosure_expires_at,
        runtime_request_id=runtime_request_id,
        runtime_mutation_id=runtime_mutation_id,
        runtime_budget_profile=runtime_budget_profile,
        runtime_question_sha256=runtime_question_sha256,
        runtime_reviewed_binding_digest=runtime_reviewed_binding_digest,
        runtime_artifact_identity=runtime_artifact_identity,
        runtime_model_run_id=runtime_model_run_id,
        evaluated_at=trusted_user_event.observed_at,
    )
    return consent


__all__ = [
    "EXTERNAL_SEND_CONSENT_VERSION",
    "ExternalSendConsent",
    "ExternalSendConsentError",
    "TRUSTED_EXTERNAL_SEND_CONSENT_VERSION",
    "TRUSTED_LOCAL_CONTROLLER_ID",
    "TRUSTED_LOCAL_CONTROLLER_VERSION",
    "TRUSTED_USER_EVENT_VERSION",
    "TrustedLocalControllerAuthority",
    "TrustedUserEventArtifact",
    "V10_F_USER_AUTHORIZATION_UTTERANCE",
    "V10_F_USER_AUTHORIZATION_TEXT_SHA256",
    "create_external_send_consent",
    "create_trusted_external_send_consent",
    "create_trusted_user_event",
    "open_v10_f_trusted_local_controller",
    "reservation_digest",
]
