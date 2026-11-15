from __future__ import annotations

import re
from dataclasses import InitVar, dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from memory.cognitive_objective import AcceptancePolicy, ObjectiveContract
from memory.cognitive_planning import CapabilityRequirement, PlanGraph
from memory.operational_state_delegation import DelegationContract
from memory.operational_state_models import (
    AuthorityArtifact,
    OperationalStateError,
    canonical_digest,
    make_identity,
    normalize_timestamp,
    validate_identity,
    validate_sha256,
)


AUTHORIZATION_CONTRACT_VERSION = 1
RISK_CLASSES = ("low", "medium", "high", "critical")
DATABASE_PERMISSIONS = ("none", "readonly", "write")

_RISK_RANK = {value: rank for rank, value in enumerate(RISK_CLASSES)}
_LOCAL_DRIVE_ROOT_RE = re.compile(r"^[A-Za-z]:[\\/]")
_COMMAND_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._:-][a-z0-9]+)*$")
_DELEGATION_TOOL_ALIASES = {
    "project.file.read": "tool:bounded-native-read",
    "project.tree.list": "tool:bounded-native-read",
    "project.text.search": "tool:bounded-native-read",
    "git.status.read": "tool:bounded-native-read",
    "git.log.read": "tool:bounded-native-read",
    "git.show.read": "tool:bounded-native-read",
    "git.diff.read": "tool:bounded-native-read",
    "database.inspect.readonly": "tool:bounded-native-read",
    "memory.retrieve": "tool:bounded-native-read",
    "operational_state.retrieve": "tool:bounded-native-read",
    "project.file.patch": "tool:bounded-local-write",
    "project.file.create": "tool:bounded-local-write",
    "project.temp.dispose_owned": "tool:bounded-local-write",
    "project.change.rollback": "tool:bounded-local-write",
    "test.run.allowlisted": "tool:bounded-process",
    "process.template.run": "tool:bounded-process",
    "git.commit.create": "tool:bounded-git-commit",
    "browser.check.readonly": "tool:bounded-handoff",
    "external.codex.handoff": "tool:bounded-handoff",
}
_AUTHORIZATION_FACTORY_SEAL = object()
_RECONCILIATION_FACTORY_SEAL = object()
_MAX_ROOTS = 64
_MAX_CAPABILITIES = 128
_MAX_COMMANDS = 64


class CognitiveAuthorizationError(OperationalStateError):
    pass


def _fail(reason_code: str) -> None:
    raise CognitiveAuthorizationError(reason_code)


def _require_bool(value: Any, reason_code: str) -> bool:
    if not isinstance(value, bool):
        _fail(reason_code)
    return value


def _require_int(
    value: Any,
    *,
    minimum: int,
    maximum: int,
    reason_code: str,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(reason_code)
    if not minimum <= value <= maximum:
        _fail(reason_code)
    return value


def _normalize_identity_list(
    values: Sequence[str],
    *,
    allow_empty: bool,
    maximum: int,
    reason_code: str,
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        _fail(reason_code)
    if len(values) > maximum:
        _fail(reason_code)
    normalized: list[str] = []
    for value in values:
        try:
            validate_identity(value, reason_code)
        except OperationalStateError:
            _fail(reason_code)
        normalized.append(value)
    result = tuple(sorted(set(normalized)))
    if len(result) != len(normalized) or (not allow_empty and not result):
        _fail(reason_code)
    return result


def _normalize_roots(
    values: Sequence[str],
    *,
    allow_empty: bool,
    reason_code: str,
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        _fail(reason_code)
    if len(values) > _MAX_ROOTS:
        _fail(reason_code)
    roots: list[str] = []
    keys: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not 3 <= len(value) <= 1024:
            _fail(reason_code)
        if "\x00" in value or value.startswith(("\\\\", "//", "\\?\\", "\\.\\")):
            _fail("authorization_nonlocal_root_denied")
        if not _LOCAL_DRIVE_ROOT_RE.match(value):
            _fail("authorization_absolute_local_root_required")
        if ":" in value[2:]:
            _fail("authorization_alternate_data_stream_denied")
        try:
            resolved = str(Path(value).resolve(strict=False))
        except (OSError, RuntimeError, ValueError):
            _fail("authorization_root_resolution_failed")
        if not _LOCAL_DRIVE_ROOT_RE.match(resolved):
            _fail("authorization_absolute_local_root_required")
        key = resolved.casefold().rstrip("\\/")
        if key in keys:
            _fail(reason_code)
        keys.add(key)
        roots.append(resolved.rstrip("\\/") or resolved)
    result = tuple(sorted(roots, key=lambda item: item.casefold()))
    if not allow_empty and not result:
        _fail(reason_code)
    return result


def _scope_allows(scopes: Sequence[str], requested: str) -> bool:
    request_key = requested.casefold().rstrip("\\/")
    for scope in scopes:
        if scope == "*":
            return True
        scope_key = scope.casefold().rstrip("\\/")
        if request_key == scope_key:
            return True
        if request_key.startswith(scope_key + "\\") or request_key.startswith(
            scope_key + "/"
        ):
            return True
    return False


def _delegation_allows_capability(
    scopes: Sequence[str],
    capability_id: str,
) -> bool:
    return (
        "*" in scopes
        or capability_id in scopes
        or _DELEGATION_TOOL_ALIASES.get(capability_id) in scopes
    )


def _normalize_capabilities(
    values: Sequence[CapabilityRequirement | Mapping[str, Any]],
) -> tuple[CapabilityRequirement, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        _fail("invalid_authorization_capabilities")
    if not values or len(values) > _MAX_CAPABILITIES:
        _fail("invalid_authorization_capabilities")
    normalized: list[CapabilityRequirement] = []
    for value in values:
        try:
            item = (
                value
                if isinstance(value, CapabilityRequirement)
                else CapabilityRequirement.from_dict(value)
            )
        except Exception:
            _fail("invalid_authorization_capabilities")
        normalized.append(item)
    result = tuple(
        sorted(
            normalized,
            key=lambda item: (
                item.capability_id,
                item.capability_version,
                item.capability_digest,
            ),
        )
    )
    identities = {
        (item.capability_id, item.capability_version) for item in result
    }
    if len(identities) != len(result):
        _fail("duplicate_authorization_capability")
    return result


def _normalize_commands(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        _fail("invalid_authorization_commands")
    if len(values) > _MAX_COMMANDS:
        _fail("invalid_authorization_commands")
    commands: list[str] = []
    for value in values:
        if not isinstance(value, str) or _COMMAND_ID_RE.fullmatch(value) is None:
            _fail("authorization_typed_command_id_required")
        commands.append(value)
    result = tuple(sorted(set(commands)))
    if len(result) != len(commands):
        _fail("duplicate_authorization_command")
    return result


def _normalize_policy_steps(
    values: Sequence[str],
    *,
    required: bool,
    reason_code: str,
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        _fail(reason_code)
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str):
            _fail(reason_code)
        item = " ".join(value.strip().split())
        if not item or len(item) > 512 or any(ord(char) < 32 for char in item):
            _fail(reason_code)
        normalized.append(item)
    result = tuple(sorted(set(normalized)))
    if len(result) != len(normalized) or (required and not result):
        _fail(reason_code)
    return result


@dataclass(frozen=True)
class CognitiveAuthorizationArtifact:
    authorization_id: str
    objective_id: str
    objective_version: int
    objective_digest: str
    plan_id: str
    plan_version: int
    plan_digest: str
    project_id: str
    delegation_id: str
    delegation_digest: str
    source_authority_id: str
    source_authority_digest: str
    issuer_kind: str
    issuer_id: str
    nonce_digest: str
    read_roots: tuple[str, ...]
    write_roots: tuple[str, ...]
    allowed_capabilities: tuple[CapabilityRequirement, ...]
    denied_capabilities: tuple[str, ...]
    allowed_commands: tuple[str, ...]
    risk_ceiling: str
    max_steps: int
    max_duration_ms: int
    max_model_calls: int
    max_tokens: int
    max_cost_microunits: int
    commit_permission: bool
    merge_permission: bool
    tag_permission: bool
    push_permission: bool
    network_permission: bool
    database_permission: str
    next_version_creation_permission: bool
    persistent_background_agent_permission: bool
    valid_from: str
    expires_at: str
    rollback_policy: tuple[str, ...]
    acceptance_policy: Mapping[str, Any]
    authority_digest: str = ""
    _factory_seal: InitVar[object | None] = None

    def __post_init__(self, _factory_seal: object | None) -> None:
        if _factory_seal is not _AUTHORIZATION_FACTORY_SEAL:
            _fail("authorization_artifact_factory_required")
        for value, code in (
            (self.authorization_id, "invalid_authorization_id"),
            (self.objective_id, "invalid_authorization_objective_id"),
            (self.plan_id, "invalid_authorization_plan_id"),
            (self.project_id, "invalid_authorization_project_id"),
            (self.delegation_id, "invalid_authorization_delegation_id"),
            (self.source_authority_id, "invalid_source_authority_id"),
            (self.issuer_id, "invalid_authorization_issuer_id"),
        ):
            try:
                validate_identity(value, code)
            except OperationalStateError:
                _fail(code)
        for value, code in (
            (self.objective_digest, "invalid_authorization_objective_digest"),
            (self.plan_digest, "invalid_authorization_plan_digest"),
            (self.delegation_digest, "invalid_authorization_delegation_digest"),
            (self.source_authority_digest, "invalid_source_authority_digest"),
            (self.nonce_digest, "invalid_authorization_nonce_digest"),
        ):
            try:
                validate_sha256(value, code)
            except OperationalStateError:
                _fail(code)
        _require_int(
            self.objective_version,
            minimum=1,
            maximum=1_000_000,
            reason_code="invalid_authorization_objective_version",
        )
        _require_int(
            self.plan_version,
            minimum=1,
            maximum=1_000_000,
            reason_code="invalid_authorization_plan_version",
        )
        if self.issuer_kind != "user" or not self.issuer_id.startswith("user:"):
            _fail("authorization_requires_trusted_user_issuer")
        object.__setattr__(
            self,
            "read_roots",
            _normalize_roots(
                self.read_roots,
                allow_empty=False,
                reason_code="invalid_authorization_read_roots",
            ),
        )
        object.__setattr__(
            self,
            "write_roots",
            _normalize_roots(
                self.write_roots,
                allow_empty=True,
                reason_code="invalid_authorization_write_roots",
            ),
        )
        if any(
            not _scope_allows(self.read_roots, root)
            for root in self.write_roots
        ):
            _fail("authorization_write_root_outside_read_scope")
        object.__setattr__(
            self,
            "allowed_capabilities",
            _normalize_capabilities(self.allowed_capabilities),
        )
        object.__setattr__(
            self,
            "denied_capabilities",
            _normalize_identity_list(
                self.denied_capabilities,
                allow_empty=True,
                maximum=_MAX_CAPABILITIES,
                reason_code="invalid_denied_capabilities",
            ),
        )
        object.__setattr__(
            self, "allowed_commands", _normalize_commands(self.allowed_commands)
        )
        allowed_ids = {item.capability_id for item in self.allowed_capabilities}
        if allowed_ids.intersection(self.denied_capabilities):
            _fail("authorization_capability_allow_deny_conflict")
        if self.risk_ceiling not in RISK_CLASSES:
            _fail("invalid_authorization_risk_ceiling")
        _require_int(
            self.max_steps,
            minimum=1,
            maximum=10_000,
            reason_code="invalid_authorization_max_steps",
        )
        _require_int(
            self.max_duration_ms,
            minimum=1,
            maximum=86_400_000,
            reason_code="invalid_authorization_max_duration",
        )
        _require_int(
            self.max_model_calls,
            minimum=0,
            maximum=10_000,
            reason_code="invalid_authorization_max_model_calls",
        )
        _require_int(
            self.max_tokens,
            minimum=0,
            maximum=1_000_000_000,
            reason_code="invalid_authorization_max_tokens",
        )
        _require_int(
            self.max_cost_microunits,
            minimum=0,
            maximum=10**15,
            reason_code="invalid_authorization_max_cost",
        )
        for field_name in (
            "commit_permission",
            "merge_permission",
            "tag_permission",
            "push_permission",
            "network_permission",
            "next_version_creation_permission",
            "persistent_background_agent_permission",
        ):
            _require_bool(
                getattr(self, field_name),
                f"invalid_authorization_{field_name}",
            )
        if (
            self.merge_permission
            or self.tag_permission
            or self.push_permission
            or self.network_permission
            or self.next_version_creation_permission
            or self.persistent_background_agent_permission
        ):
            _fail("authorization_first_release_permission_denied")
        if self.database_permission not in {"none", "readonly"}:
            _fail("authorization_database_write_denied")
        database_inspection_allowed = any(
            item.capability_id == "database.inspect.readonly"
            for item in self.allowed_capabilities
        )
        if database_inspection_allowed and self.database_permission != "readonly":
            _fail("authorization_database_read_permission_required")
        object.__setattr__(
            self, "valid_from", normalize_timestamp(self.valid_from)
        )
        object.__setattr__(self, "expires_at", normalize_timestamp(self.expires_at))
        if self.expires_at <= self.valid_from:
            _fail("invalid_authorization_interval")
        object.__setattr__(
            self,
            "rollback_policy",
            _normalize_policy_steps(
                self.rollback_policy,
                required=bool(self.write_roots),
                reason_code="invalid_authorization_rollback_policy",
            ),
        )
        try:
            acceptance = AcceptancePolicy.from_dict(dict(self.acceptance_policy))
        except Exception:
            _fail("invalid_authorization_acceptance_policy")
        object.__setattr__(
            self,
            "acceptance_policy",
            MappingProxyType(acceptance.to_dict()),
        )
        expected = canonical_digest(self.identity_payload())
        if self.authority_digest and self.authority_digest != expected:
            _fail("authorization_digest_mismatch")
        object.__setattr__(self, "authority_digest", expected)

    def identity_payload(self) -> dict[str, Any]:
        return {
            "contract_version": AUTHORIZATION_CONTRACT_VERSION,
            "authorization_id": self.authorization_id,
            "objective_id": self.objective_id,
            "objective_version": self.objective_version,
            "objective_digest": self.objective_digest,
            "plan_id": self.plan_id,
            "plan_version": self.plan_version,
            "plan_digest": self.plan_digest,
            "project_id": self.project_id,
            "delegation_id": self.delegation_id,
            "delegation_digest": self.delegation_digest,
            "source_authority_id": self.source_authority_id,
            "source_authority_digest": self.source_authority_digest,
            "issuer_kind": self.issuer_kind,
            "issuer_id": self.issuer_id,
            "nonce_digest": self.nonce_digest,
            "read_roots": list(self.read_roots),
            "write_roots": list(self.write_roots),
            "allowed_capabilities": [
                item.to_dict() for item in self.allowed_capabilities
            ],
            "denied_capabilities": list(self.denied_capabilities),
            "allowed_commands": list(self.allowed_commands),
            "risk_ceiling": self.risk_ceiling,
            "max_steps": self.max_steps,
            "max_duration_ms": self.max_duration_ms,
            "max_model_calls": self.max_model_calls,
            "max_tokens": self.max_tokens,
            "max_cost_microunits": self.max_cost_microunits,
            "commit_permission": self.commit_permission,
            "merge_permission": self.merge_permission,
            "tag_permission": self.tag_permission,
            "push_permission": self.push_permission,
            "network_permission": self.network_permission,
            "database_permission": self.database_permission,
            "next_version_creation_permission": self.next_version_creation_permission,
            "persistent_background_agent_permission": self.persistent_background_agent_permission,
            "valid_from": self.valid_from,
            "expires_at": self.expires_at,
            "rollback_policy": list(self.rollback_policy),
            "acceptance_policy": dict(self.acceptance_policy),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_payload(), "authority_digest": self.authority_digest}

    def allows_capability(
        self,
        capability_id: str,
        capability_version: int,
        capability_digest: str,
    ) -> bool:
        if capability_id in self.denied_capabilities:
            return False
        return any(
            item.capability_id == capability_id
            and item.capability_version == capability_version
            and item.capability_digest == capability_digest
            for item in self.allowed_capabilities
        )

    def assert_current(self, at: str) -> None:
        timestamp = normalize_timestamp(at)
        if not self.valid_from <= timestamp < self.expires_at:
            _fail("authorization_expired_or_not_yet_valid")


@dataclass(frozen=True)
class SystemReconciliationAuthority:
    """Ephemeral local-broker authority for closing one durable reservation."""

    authority_id: str
    issuer_kind: str
    issuer_id: str
    project_id: str
    run_id: str
    original_authorization_digest: str
    reservation_event_id: str
    reservation_receipt_digest: str
    execution_key: str
    proposal_digest: str
    preview_digest: str
    allowed_operations: tuple[str, ...]
    valid_from: str
    expires_at: str
    max_writes: int
    lease_owner_id: str
    lease_token_digest: str
    nonce_digest: str
    authority_digest: str = ""
    _factory_seal: InitVar[object | None] = None

    def __post_init__(self, _factory_seal: object | None) -> None:
        if _factory_seal is not _RECONCILIATION_FACTORY_SEAL:
            _fail("system_reconciliation_authority_factory_required")
        for value, code in (
            (self.authority_id, "invalid_reconciliation_authority_id"),
            (self.issuer_id, "invalid_reconciliation_issuer_id"),
            (self.project_id, "invalid_reconciliation_project_id"),
            (self.run_id, "invalid_reconciliation_run_id"),
            (self.reservation_event_id, "invalid_reconciliation_event_id"),
            (self.execution_key, "invalid_reconciliation_execution_key"),
            (self.lease_owner_id, "invalid_reconciliation_lease_owner"),
        ):
            try:
                validate_identity(value, code)
            except OperationalStateError:
                _fail(code)
        if (
            self.issuer_kind != "local_broker"
            or self.issuer_id != "runtime:yunos-reconciliation"
        ):
            _fail("invalid_reconciliation_issuer")
        for value, code in (
            (
                self.original_authorization_digest,
                "invalid_reconciliation_original_authority_digest",
            ),
            (
                self.reservation_receipt_digest,
                "invalid_reconciliation_receipt_digest",
            ),
            (self.proposal_digest, "invalid_reconciliation_proposal_digest"),
            (self.preview_digest, "invalid_reconciliation_preview_digest"),
            (self.lease_token_digest, "invalid_reconciliation_lease_digest"),
            (self.nonce_digest, "invalid_reconciliation_nonce_digest"),
        ):
            try:
                validate_sha256(value, code)
            except OperationalStateError:
                _fail(code)
        operations = tuple(sorted(set(self.allowed_operations)))
        allowed = {
            "record_reserved_terminal",
            "mark_recovery_required",
            "append_recovery_supervision",
        }
        if not operations or not set(operations).issubset(allowed):
            _fail("reconciliation_scope_violation")
        object.__setattr__(self, "allowed_operations", operations)
        if self.max_writes != 1:
            _fail("invalid_reconciliation_write_budget")
        object.__setattr__(self, "valid_from", normalize_timestamp(self.valid_from))
        object.__setattr__(self, "expires_at", normalize_timestamp(self.expires_at))
        if self.expires_at <= self.valid_from:
            _fail("invalid_reconciliation_interval")
        expected = canonical_digest(self.identity_payload())
        if self.authority_digest and self.authority_digest != expected:
            _fail("reconciliation_authority_digest_mismatch")
        object.__setattr__(self, "authority_digest", expected)

    def identity_payload(self) -> dict[str, Any]:
        return {
            "contract_version": 1,
            "authority_id": self.authority_id,
            "issuer_kind": self.issuer_kind,
            "issuer_id": self.issuer_id,
            "project_id": self.project_id,
            "run_id": self.run_id,
            "original_authorization_digest": self.original_authorization_digest,
            "reservation_event_id": self.reservation_event_id,
            "reservation_receipt_digest": self.reservation_receipt_digest,
            "execution_key": self.execution_key,
            "proposal_digest": self.proposal_digest,
            "preview_digest": self.preview_digest,
            "allowed_operations": list(self.allowed_operations),
            "valid_from": self.valid_from,
            "expires_at": self.expires_at,
            "max_writes": self.max_writes,
            "lease_owner_id": self.lease_owner_id,
            "lease_token_digest": self.lease_token_digest,
            "nonce_digest": self.nonce_digest,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_payload(), "authority_digest": self.authority_digest}

    def authorize(self, operation: str, at: str) -> None:
        if operation not in self.allowed_operations:
            _fail("reconciliation_scope_violation")
        timestamp = normalize_timestamp(at)
        if not self.valid_from <= timestamp < self.expires_at:
            _fail("reconciliation_authority_expired")


def _issue_system_reconciliation_authority(**values: Any) -> SystemReconciliationAuthority:
    return SystemReconciliationAuthority(
        **values,
        _factory_seal=_RECONCILIATION_FACTORY_SEAL,
    )


@dataclass(frozen=True)
class SystemModelReconciliationAuthority:
    """One-write authority that may only quarantine one unresolved model call."""

    authority_id: str
    issuer_kind: str
    issuer_id: str
    project_id: str
    run_id: str
    original_authorization_digest: str
    reservation_event_id: str
    reservation_receipt_digest: str
    reservation_id: str
    reservation_digest: str
    request_digest: str
    provider_profile_digest: str
    allowed_operations: tuple[str, ...]
    valid_from: str
    expires_at: str
    max_writes: int
    lease_owner_id: str
    lease_token_digest: str
    nonce_digest: str
    authority_digest: str = ""
    _factory_seal: InitVar[object | None] = None
    _factory_marker: object = field(init=False, repr=False, compare=False)

    def __post_init__(self, _factory_seal: object | None) -> None:
        if _factory_seal is not _RECONCILIATION_FACTORY_SEAL:
            _fail("system_model_reconciliation_authority_factory_required")
        object.__setattr__(
            self,
            "_factory_marker",
            _RECONCILIATION_FACTORY_SEAL,
        )
        for value, code in (
            (self.authority_id, "invalid_model_reconciliation_authority_id"),
            (self.issuer_id, "invalid_model_reconciliation_issuer_id"),
            (self.project_id, "invalid_model_reconciliation_project_id"),
            (self.run_id, "invalid_model_reconciliation_run_id"),
            (self.reservation_event_id, "invalid_model_reconciliation_event_id"),
            (self.reservation_id, "invalid_model_reconciliation_reservation_id"),
            (self.lease_owner_id, "invalid_model_reconciliation_lease_owner"),
        ):
            try:
                validate_identity(value, code)
            except OperationalStateError:
                _fail(code)
        if (
            self.issuer_kind != "local_broker"
            or self.issuer_id != "runtime:yunos-model-reconciliation"
        ):
            _fail("invalid_model_reconciliation_issuer")
        for value, code in (
            (self.original_authorization_digest, "invalid_model_reconciliation_authority_digest"),
            (self.reservation_receipt_digest, "invalid_model_reconciliation_receipt_digest"),
            (self.reservation_digest, "invalid_model_reconciliation_reservation_digest"),
            (self.request_digest, "invalid_model_reconciliation_request_digest"),
            (self.provider_profile_digest, "invalid_model_reconciliation_provider_digest"),
            (self.lease_token_digest, "invalid_model_reconciliation_lease_digest"),
            (self.nonce_digest, "invalid_model_reconciliation_nonce_digest"),
        ):
            try:
                validate_sha256(value, code)
            except OperationalStateError:
                _fail(code)
        operations = tuple(sorted(set(self.allowed_operations)))
        if operations != ("quarantine_model_reservation",):
            _fail("model_reconciliation_scope_violation")
        object.__setattr__(self, "allowed_operations", operations)
        if self.max_writes != 1:
            _fail("invalid_model_reconciliation_write_budget")
        object.__setattr__(self, "valid_from", normalize_timestamp(self.valid_from))
        object.__setattr__(self, "expires_at", normalize_timestamp(self.expires_at))
        if self.expires_at <= self.valid_from:
            _fail("invalid_model_reconciliation_interval")
        expected = canonical_digest(self.identity_payload())
        if self.authority_digest and self.authority_digest != expected:
            _fail("model_reconciliation_authority_digest_mismatch")
        object.__setattr__(self, "authority_digest", expected)

    def identity_payload(self) -> dict[str, Any]:
        return {
            "contract_version": 1,
            "authority_id": self.authority_id,
            "issuer_kind": self.issuer_kind,
            "issuer_id": self.issuer_id,
            "project_id": self.project_id,
            "run_id": self.run_id,
            "original_authorization_digest": self.original_authorization_digest,
            "reservation_event_id": self.reservation_event_id,
            "reservation_receipt_digest": self.reservation_receipt_digest,
            "reservation_id": self.reservation_id,
            "reservation_digest": self.reservation_digest,
            "request_digest": self.request_digest,
            "provider_profile_digest": self.provider_profile_digest,
            "allowed_operations": list(self.allowed_operations),
            "valid_from": self.valid_from,
            "expires_at": self.expires_at,
            "max_writes": self.max_writes,
            "lease_owner_id": self.lease_owner_id,
            "lease_token_digest": self.lease_token_digest,
            "nonce_digest": self.nonce_digest,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_payload(), "authority_digest": self.authority_digest}

    def authorize(self, operation: str, at: str) -> None:
        self.assert_factory_sealed()
        if operation not in self.allowed_operations:
            _fail("model_reconciliation_scope_violation")
        timestamp = normalize_timestamp(at)
        if not self.valid_from <= timestamp < self.expires_at:
            _fail("model_reconciliation_authority_expired")

    def assert_factory_sealed(self) -> None:
        if (
            type(self) is not SystemModelReconciliationAuthority
            or getattr(self, "_factory_marker", None)
            is not _RECONCILIATION_FACTORY_SEAL
        ):
            _fail("system_model_reconciliation_authority_factory_required")
        if canonical_digest(self.identity_payload()) != self.authority_digest:
            _fail("model_reconciliation_authority_digest_mismatch")


def _issue_system_model_reconciliation_authority(
    **values: Any,
) -> SystemModelReconciliationAuthority:
    return SystemModelReconciliationAuthority(
        **values,
        _factory_seal=_RECONCILIATION_FACTORY_SEAL,
    )


def issue_authorization_artifact(
    *,
    objective: ObjectiveContract,
    plan: PlanGraph,
    delegation: DelegationContract,
    source_authority: AuthorityArtifact,
    read_roots: Sequence[str],
    write_roots: Sequence[str],
    allowed_capabilities: Sequence[CapabilityRequirement | Mapping[str, Any]],
    denied_capabilities: Sequence[str] = (),
    allowed_commands: Sequence[str] = (),
    risk_ceiling: str = "low",
    max_steps: int = 32,
    max_duration_ms: int = 1_200_000,
    max_model_calls: int = 0,
    max_tokens: int = 0,
    max_cost_microunits: int = 0,
    commit_permission: bool = False,
    database_permission: str = "none",
    valid_from: str,
    expires_at: str,
    rollback_policy: Sequence[str] = (),
    nonce_material: str,
) -> CognitiveAuthorizationArtifact:
    if not isinstance(objective, ObjectiveContract):
        _fail("invalid_authorization_objective")
    if not isinstance(plan, PlanGraph):
        _fail("invalid_authorization_plan")
    if not isinstance(delegation, DelegationContract):
        _fail("invalid_authorization_delegation")
    if not isinstance(source_authority, AuthorityArtifact):
        _fail("invalid_source_authority")
    if objective.agency_level != "act":
        _fail("act_authorization_requires_act_objective")
    if not source_authority.issued_by.startswith("user:"):
        _fail("authorization_requires_trusted_user_issuer")
    if objective.project_id != source_authority.target_project_id:
        _fail("authorization_project_mismatch")
    if delegation.project_id != objective.project_id:
        _fail("authorization_delegation_project_mismatch")
    if delegation.authority_digest != source_authority.authority_digest:
        _fail("authorization_source_authority_mismatch")
    if (
        plan.objective_id != objective.objective_id
        or plan.objective_version != objective.version
        or plan.objective_digest != objective.canonical_digest
    ):
        _fail("authorization_plan_objective_mismatch")
    normalized_from = normalize_timestamp(valid_from)
    normalized_expires = normalize_timestamp(expires_at)
    if (
        normalized_from < source_authority.issued_at
        or normalized_from < delegation.valid_from
        or normalized_expires > source_authority.expires_at
        or normalized_expires > delegation.expires_at
    ):
        _fail("authorization_exceeds_source_interval")
    normalized_reads = _normalize_roots(
        read_roots,
        allow_empty=False,
        reason_code="invalid_authorization_read_roots",
    )
    normalized_writes = _normalize_roots(
        write_roots,
        allow_empty=True,
        reason_code="invalid_authorization_write_roots",
    )
    for root in normalized_reads:
        if not _scope_allows(delegation.read_scope, root) or not _scope_allows(
            source_authority.read_scope, root
        ):
            _fail("authorization_exceeds_read_scope")
    for root in normalized_writes:
        if not _scope_allows(delegation.write_scope, root) or not _scope_allows(
            source_authority.write_scope, root
        ):
            _fail("authorization_exceeds_write_scope")
    normalized_caps = _normalize_capabilities(allowed_capabilities)
    if any(
        not _delegation_allows_capability(
            delegation.tool_scope,
            item.capability_id,
        )
        for item in normalized_caps
    ):
        _fail("authorization_exceeds_delegation_tool_scope")
    risk_order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    if risk_ceiling not in risk_order:
        _fail("invalid_authorization_risk_ceiling")
    if risk_order[risk_ceiling] > risk_order[delegation.risk]:
        _fail("authorization_exceeds_delegation_risk")
    allowed_keys = {
        (item.capability_id, item.capability_version, item.capability_digest)
        for item in normalized_caps
    }
    for node in plan.nodes:
        for requirement in node.capability_requirements:
            key = (
                requirement.capability_id,
                requirement.capability_version,
                requirement.capability_digest,
            )
            if key not in allowed_keys:
                _fail("authorization_missing_plan_capability")
    if commit_permission and (
        not source_authority.commit_permission
        or not delegation.commit_permission
        or "commit" in source_authority.prohibited_operations
        or "commit" in delegation.prohibited_actions
    ):
        _fail("authorization_commit_exceeds_source")
    if database_permission == "write":
        _fail("authorization_database_write_denied")
    if (
        any(
            item.capability_id == "database.inspect.readonly"
            for item in normalized_caps
        )
        and database_permission != "readonly"
    ):
        _fail("authorization_database_read_permission_required")
    if not isinstance(nonce_material, str) or not 16 <= len(nonce_material) <= 1024:
        _fail("invalid_authorization_nonce_material")
    nonce_digest = canonical_digest(
        {
            "nonce_material": nonce_material,
            "objective_digest": objective.canonical_digest,
            "plan_digest": plan.plan_digest,
            "source_authority_digest": source_authority.authority_digest,
            "valid_from": normalized_from,
            "expires_at": normalized_expires,
        }
    )
    identity = {
        "objective_digest": objective.canonical_digest,
        "plan_digest": plan.plan_digest,
        "delegation_id": delegation.delegation_id,
        "source_authority_digest": source_authority.authority_digest,
        "nonce_digest": nonce_digest,
    }
    return CognitiveAuthorizationArtifact(
        authorization_id=make_identity("authorization", identity),
        objective_id=objective.objective_id,
        objective_version=objective.version,
        objective_digest=objective.canonical_digest,
        plan_id=plan.plan_id,
        plan_version=plan.version,
        plan_digest=plan.plan_digest,
        project_id=objective.project_id,
        delegation_id=delegation.delegation_id,
        delegation_digest=delegation.contract_digest,
        source_authority_id=source_authority.authority_id,
        source_authority_digest=source_authority.authority_digest,
        issuer_kind="user",
        issuer_id=source_authority.issued_by,
        nonce_digest=nonce_digest,
        read_roots=normalized_reads,
        write_roots=normalized_writes,
        allowed_capabilities=normalized_caps,
        denied_capabilities=tuple(denied_capabilities),
        allowed_commands=tuple(allowed_commands),
        risk_ceiling=risk_ceiling,
        max_steps=max_steps,
        max_duration_ms=max_duration_ms,
        max_model_calls=max_model_calls,
        max_tokens=max_tokens,
        max_cost_microunits=max_cost_microunits,
        commit_permission=commit_permission,
        merge_permission=False,
        tag_permission=False,
        push_permission=False,
        network_permission=False,
        database_permission=database_permission,
        next_version_creation_permission=False,
        persistent_background_agent_permission=False,
        valid_from=normalized_from,
        expires_at=normalized_expires,
        rollback_policy=tuple(rollback_policy),
        acceptance_policy=objective.acceptance_policy.to_dict(),
        _factory_seal=_AUTHORIZATION_FACTORY_SEAL,
    )


def _authorization_from_verified_record(
    value: Mapping[str, Any],
) -> CognitiveAuthorizationArtifact:
    if not isinstance(value, Mapping):
        _fail("invalid_authorization_record")
    payload = dict(value)
    if payload.pop("contract_version", None) != AUTHORIZATION_CONTRACT_VERSION:
        _fail("invalid_authorization_contract_version")
    for name in (
        "read_roots",
        "write_roots",
        "allowed_capabilities",
        "denied_capabilities",
        "allowed_commands",
        "rollback_policy",
    ):
        if not isinstance(payload.get(name), list):
            _fail("invalid_authorization_record")
        payload[name] = tuple(payload[name])
    payload["allowed_capabilities"] = tuple(
        CapabilityRequirement.from_dict(item)
        for item in payload["allowed_capabilities"]
    )
    payload["_factory_seal"] = _AUTHORIZATION_FACTORY_SEAL
    try:
        return CognitiveAuthorizationArtifact(**payload)
    except TypeError:
        _fail("invalid_authorization_record")


__all__ = [
    "AUTHORIZATION_CONTRACT_VERSION",
    "CognitiveAuthorizationArtifact",
    "CognitiveAuthorizationError",
    "SystemModelReconciliationAuthority",
    "SystemReconciliationAuthority",
    "issue_authorization_artifact",
]
