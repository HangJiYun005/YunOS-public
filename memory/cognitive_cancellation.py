from __future__ import annotations

from dataclasses import InitVar, dataclass, field
from typing import Any, Mapping

from memory.operational_state_models import (
    AuthorityArtifact,
    OperationalStateError,
    canonical_digest as compute_canonical_digest,
    make_identity,
    normalize_timestamp,
    validate_identity,
    validate_sha256,
)


CANCELLATION_ARTIFACT_VERSION = 1
CANCELLATION_OPERATION = "cancel_run"

_CANCELLATION_FACTORY_SEAL = object()
_MAX_REASON_LENGTH = 512


class CognitiveCancellationError(OperationalStateError):
    pass


def _fail(reason_code: str) -> None:
    raise CognitiveCancellationError(reason_code)


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


def _reason(value: Any) -> str:
    if not isinstance(value, str):
        _fail("invalid_cancellation_reason")
    normalized = " ".join(value.strip().split())
    if (
        not normalized
        or len(normalized) > _MAX_REASON_LENGTH
        or any(ord(character) < 32 for character in normalized)
    ):
        _fail("invalid_cancellation_reason")
    return normalized


@dataclass(frozen=True)
class UserCancellationArtifact:
    cancellation_id: str
    project_id: str
    run_id: str
    expected_run_digest: str
    user_authority_id: str
    user_authority_digest: str
    user_actor: str
    session_digest: str
    csrf_session_proof_digest: str
    requested_at: str
    expires_at: str
    reason: str
    allowed_operation: str = CANCELLATION_OPERATION
    project_mutation_permission: bool = False
    git_commit_permission: bool = False
    merge_permission: bool = False
    tag_permission: bool = False
    push_permission: bool = False
    network_permission: bool = False
    database_write_permission: bool = False
    acceptance_permission: bool = False
    next_version_creation_permission: bool = False
    persistent_background_agent_permission: bool = False
    version: int = CANCELLATION_ARTIFACT_VERSION
    canonical_digest: str = ""
    _factory_seal: InitVar[object | None] = None
    _factory_marker: object = field(init=False, repr=False, compare=False)

    def __post_init__(self, _factory_seal: object | None) -> None:
        if _factory_seal is not _CANCELLATION_FACTORY_SEAL:
            _fail("cancellation_artifact_factory_required")
        object.__setattr__(self, "_factory_marker", _CANCELLATION_FACTORY_SEAL)
        for name in (
            "cancellation_id",
            "project_id",
            "run_id",
            "user_authority_id",
            "user_actor",
        ):
            object.__setattr__(
                self,
                name,
                _identity(getattr(self, name), f"invalid_cancellation_{name}"),
            )
        for name in (
            "expected_run_digest",
            "user_authority_digest",
            "session_digest",
            "csrf_session_proof_digest",
        ):
            object.__setattr__(
                self,
                name,
                _digest(getattr(self, name), f"invalid_cancellation_{name}"),
            )
        requested_at = _timestamp(
            self.requested_at,
            "invalid_cancellation_requested_at",
        )
        expires_at = _timestamp(
            self.expires_at,
            "invalid_cancellation_expires_at",
        )
        if expires_at <= requested_at:
            _fail("invalid_cancellation_interval")
        object.__setattr__(self, "requested_at", requested_at)
        object.__setattr__(self, "expires_at", expires_at)
        object.__setattr__(self, "reason", _reason(self.reason))
        if self.allowed_operation != CANCELLATION_OPERATION:
            _fail("cancellation_operation_scope_violation")
        permission_fields = (
            "project_mutation_permission",
            "git_commit_permission",
            "merge_permission",
            "tag_permission",
            "push_permission",
            "network_permission",
            "database_write_permission",
            "acceptance_permission",
            "next_version_creation_permission",
            "persistent_background_agent_permission",
        )
        if any(getattr(self, name) is not False for name in permission_fields):
            _fail("cancellation_permission_expansion_denied")
        if (
            isinstance(self.version, bool)
            or self.version != CANCELLATION_ARTIFACT_VERSION
        ):
            _fail("invalid_cancellation_artifact_version")
        expected = compute_canonical_digest(self.identity_payload())
        if self.canonical_digest:
            supplied = _digest(
                self.canonical_digest,
                "invalid_cancellation_canonical_digest",
            )
            if supplied != expected:
                _fail("cancellation_digest_mismatch")
        object.__setattr__(self, "canonical_digest", expected)

    def identity_payload(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "cancellation_id": self.cancellation_id,
            "project_id": self.project_id,
            "run_id": self.run_id,
            "expected_run_digest": self.expected_run_digest,
            "user_authority_id": self.user_authority_id,
            "user_authority_digest": self.user_authority_digest,
            "user_actor": self.user_actor,
            "session_digest": self.session_digest,
            "csrf_session_proof_digest": self.csrf_session_proof_digest,
            "requested_at": self.requested_at,
            "expires_at": self.expires_at,
            "reason": self.reason,
            "allowed_operation": self.allowed_operation,
            "project_mutation_permission": self.project_mutation_permission,
            "git_commit_permission": self.git_commit_permission,
            "merge_permission": self.merge_permission,
            "tag_permission": self.tag_permission,
            "push_permission": self.push_permission,
            "network_permission": self.network_permission,
            "database_write_permission": self.database_write_permission,
            "acceptance_permission": self.acceptance_permission,
            "next_version_creation_permission": (
                self.next_version_creation_permission
            ),
            "persistent_background_agent_permission": (
                self.persistent_background_agent_permission
            ),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.identity_payload(),
            "canonical_digest": self.canonical_digest,
        }

    def assert_valid_for(
        self,
        *,
        project_id: str,
        run_id: str,
        expected_run_digest: str,
        at: str,
    ) -> None:
        if getattr(self, "_factory_marker", None) is not _CANCELLATION_FACTORY_SEAL:
            _fail("cancellation_artifact_factory_required")
        if compute_canonical_digest(self.identity_payload()) != self.canonical_digest:
            _fail("cancellation_digest_mismatch")
        current = _timestamp(at, "invalid_cancellation_evaluation_time")
        if not self.requested_at <= current < self.expires_at:
            _fail("cancellation_expired_or_not_yet_valid")
        if project_id != self.project_id:
            _fail("cancellation_project_mismatch")
        if run_id != self.run_id:
            _fail("cancellation_run_mismatch")
        expected = _digest(
            expected_run_digest,
            "invalid_cancellation_expected_run_digest",
        )
        if expected != self.expected_run_digest:
            _fail("cancellation_run_digest_mismatch")


def issue_user_cancellation_artifact(
    *,
    source_authority: AuthorityArtifact,
    project_id: str,
    run_id: str,
    expected_run_digest: str,
    session_digest: str,
    csrf_session_proof_digest: str,
    requested_at: str,
    expires_at: str,
    reason: str,
) -> UserCancellationArtifact:
    if not isinstance(source_authority, AuthorityArtifact):
        _fail("typed_user_authority_required")
    normalized_project = _identity(project_id, "invalid_cancellation_project_id")
    normalized_run = _identity(run_id, "invalid_cancellation_run_id")
    normalized_run_digest = _digest(
        expected_run_digest,
        "invalid_cancellation_expected_run_digest",
    )
    normalized_session_digest = _digest(
        session_digest,
        "invalid_cancellation_session_digest",
    )
    normalized_proof_digest = _digest(
        csrf_session_proof_digest,
        "invalid_cancellation_csrf_session_proof_digest",
    )
    normalized_requested_at = _timestamp(
        requested_at,
        "invalid_cancellation_requested_at",
    )
    normalized_expires_at = _timestamp(
        expires_at,
        "invalid_cancellation_expires_at",
    )
    normalized_reason = _reason(reason)

    if (
        compute_canonical_digest(source_authority.identity_payload())
        != source_authority.authority_digest
    ):
        _fail("user_authority_digest_mismatch")
    if not source_authority.issued_by.startswith("user:"):
        _fail("cancellation_requires_trusted_user_issuer")
    if source_authority.target_project_id != normalized_project:
        _fail("cancellation_project_mismatch")
    if not (
        source_authority.issued_at
        <= normalized_requested_at
        < source_authority.expires_at
    ):
        _fail("user_authority_expired_or_not_yet_valid")
    if (
        normalized_expires_at <= normalized_requested_at
        or normalized_expires_at > source_authority.expires_at
    ):
        _fail("cancellation_exceeds_user_authority_interval")
    if not source_authority.commit_permission:
        _fail("cancellation_event_commit_not_authorized")
    if (
        normalized_run not in source_authority.write_scope
        and "*" not in source_authority.write_scope
    ):
        _fail("cancellation_run_write_not_authorized")

    identity_seed = {
        "version": CANCELLATION_ARTIFACT_VERSION,
        "project_id": normalized_project,
        "run_id": normalized_run,
        "expected_run_digest": normalized_run_digest,
        "user_authority_id": source_authority.authority_id,
        "user_authority_digest": source_authority.authority_digest,
        "user_actor": source_authority.issued_by,
        "session_digest": normalized_session_digest,
        "csrf_session_proof_digest": normalized_proof_digest,
        "requested_at": normalized_requested_at,
        "expires_at": normalized_expires_at,
        "reason": normalized_reason,
        "allowed_operation": CANCELLATION_OPERATION,
    }
    return UserCancellationArtifact(
        cancellation_id=make_identity("cancellation", identity_seed),
        project_id=normalized_project,
        run_id=normalized_run,
        expected_run_digest=normalized_run_digest,
        user_authority_id=source_authority.authority_id,
        user_authority_digest=source_authority.authority_digest,
        user_actor=source_authority.issued_by,
        session_digest=normalized_session_digest,
        csrf_session_proof_digest=normalized_proof_digest,
        requested_at=normalized_requested_at,
        expires_at=normalized_expires_at,
        reason=normalized_reason,
        _factory_seal=_CANCELLATION_FACTORY_SEAL,
    )


def _cancellation_from_verified_record(
    value: Mapping[str, Any],
) -> UserCancellationArtifact:
    if not isinstance(value, Mapping):
        _fail("invalid_cancellation_record")
    expected_fields = {
        "version",
        "cancellation_id",
        "project_id",
        "run_id",
        "expected_run_digest",
        "user_authority_id",
        "user_authority_digest",
        "user_actor",
        "session_digest",
        "csrf_session_proof_digest",
        "requested_at",
        "expires_at",
        "reason",
        "allowed_operation",
        "project_mutation_permission",
        "git_commit_permission",
        "merge_permission",
        "tag_permission",
        "push_permission",
        "network_permission",
        "database_write_permission",
        "acceptance_permission",
        "next_version_creation_permission",
        "persistent_background_agent_permission",
        "canonical_digest",
    }
    if set(value) != expected_fields:
        _fail("invalid_cancellation_record")
    try:
        return UserCancellationArtifact(
            **dict(value),
            _factory_seal=_CANCELLATION_FACTORY_SEAL,
        )
    except TypeError:
        _fail("invalid_cancellation_record")


__all__ = [
    "CANCELLATION_ARTIFACT_VERSION",
    "CANCELLATION_OPERATION",
    "CognitiveCancellationError",
    "UserCancellationArtifact",
    "issue_user_cancellation_artifact",
]
