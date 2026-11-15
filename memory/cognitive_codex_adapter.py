"""Pure, handoff-only Codex adapter contracts for YunOS v10-D.

The local v10-D audit found an app-scoped Codex desktop binary but no
supported CLI entry point that the current PowerShell token can execute.  This
module therefore exposes only a code-owned audit snapshot and a minimal handoff
artifact.  It performs no process, file, environment, credential, database,
Provider, Runtime, or network I/O.

A handoff artifact is untrusted transfer metadata.  It is not an
Authorization Artifact, an execution receipt, system verification, or user
acceptance.  Every execution attempt fails closed.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import InitVar, dataclass
import re
import unicodedata
from typing import Any

from memory.cognitive_orchestration import contains_secret_material
from memory.operational_state_models import (
    OperationalStateError,
    canonical_digest,
    validate_sha256,
)


CODEX_AUDIT_CONTRACT_VERSION = "yunos-v10-d-codex-audit-1"
CODEX_HANDOFF_CONTRACT_VERSION = "yunos-v10-d-codex-handoff-1"

CODEX_HANDOFF_CONTEXT_KINDS = (
    "project_artifact",
    "source_anchor",
    "v7_memory_package",
    "v9_now_package",
    "v9_resume_package",
    "verification_artifact",
)

CODEX_HANDOFF_MANDATORY_PROHIBITIONS = (
    "arbitrary_shell",
    "authority_escalation",
    "credential_or_secret_access",
    "database_write",
    "git_clean",
    "git_merge",
    "git_push",
    "git_rebase",
    "git_reset_hard",
    "git_tag",
    "network_access",
    "next_version_creation",
    "persistent_background_agent",
    "self_acceptance",
    "write_outside_authorized_roots",
)

_AUDIT_FACTORY_SEAL = object()
_CONTEXT_FACTORY_SEAL = object()
_HANDOFF_FACTORY_SEAL = object()
_ADAPTER_FACTORY_SEAL = object()
_MAX_CONTEXT_REFERENCES = 16
_MAX_AUTHORIZED_ROOTS = 16
_MAX_PROHIBITED_OPERATIONS = 64
_OPERATION_RE = re.compile(r"^[a-z][a-z0-9_.-]{2,63}$")
_WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "aux",
        "con",
        "nul",
        "prn",
        *(f"com{number}" for number in range(1, 10)),
        *(f"lpt{number}" for number in range(1, 10)),
    }
)


class CodexAdapterError(ValueError):
    """Sanitized, fail-closed Codex adapter contract error."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def _fail(reason_code: str) -> None:
    raise CodexAdapterError(reason_code)


def _digest(value: Any, reason_code: str) -> str:
    try:
        return validate_sha256(value, reason_code)
    except OperationalStateError:
        _fail(reason_code)


def _items(value: Any, reason_code: str) -> tuple[Any, ...]:
    if (
        isinstance(value, (str, bytes, Mapping))
        or not isinstance(value, Iterable)
    ):
        _fail(reason_code)
    return tuple(value)


def _reject_secret(value: Any) -> None:
    if contains_secret_material(value):
        _fail("codex_handoff_secret_forbidden")


def _normalize_windows_root(value: Any) -> str:
    if not isinstance(value, str):
        _fail("invalid_codex_authorized_root")
    _reject_secret(value)
    normalized = unicodedata.normalize("NFC", value)
    if (
        not normalized
        or normalized != normalized.strip()
        or len(normalized) > 512
        or "\x00" in normalized
        or any(ord(character) < 32 for character in normalized)
        or not _WINDOWS_ABSOLUTE_RE.match(normalized)
        or normalized.startswith(("\\\\", "//"))
    ):
        _fail("invalid_codex_authorized_root")
    normalized = normalized.replace("/", "\\")
    if normalized.startswith(("\\\\?\\", "\\\\.\\")):
        _fail("invalid_codex_authorized_root")
    if len(normalized) <= 3 or ":" in normalized[2:]:
        _fail("invalid_codex_authorized_root")
    components = normalized[3:].split("\\")
    if any(
        not component
        or component in {".", ".."}
        or component.endswith((" ", "."))
        or any(character in '<>:"|?*' for character in component)
        or component.split(".", 1)[0].casefold()
        in _WINDOWS_RESERVED_NAMES
        for component in components
    ):
        _fail("invalid_codex_authorized_root")
    return normalized[0].upper() + normalized[1:]


def _normalize_roots(
    value: Any,
    *,
    required: bool,
) -> tuple[str, ...]:
    raw = _items(value, "invalid_codex_authorized_roots")
    if (required and not raw) or len(raw) > _MAX_AUTHORIZED_ROOTS:
        _fail("invalid_codex_authorized_roots")
    _reject_secret(raw)
    roots = tuple(_normalize_windows_root(item) for item in raw)
    keys = tuple(root.casefold() for root in roots)
    if len(set(keys)) != len(keys):
        _fail("duplicate_codex_authorized_root")
    return tuple(sorted(roots, key=lambda root: (root.casefold(), root)))


def _root_is_within(root: str, read_root: str) -> bool:
    candidate = root.casefold()
    parent = read_root.casefold()
    return candidate == parent or candidate.startswith(parent + "\\")


def _validate_write_roots(
    read_roots: tuple[str, ...],
    write_roots: tuple[str, ...],
) -> None:
    if any(
        not any(_root_is_within(write_root, read_root) for read_root in read_roots)
        for write_root in write_roots
    ):
        _fail("codex_write_root_outside_read_roots")


def _normalize_operation(value: Any) -> str:
    if not isinstance(value, str) or _OPERATION_RE.fullmatch(value) is None:
        _fail("invalid_codex_prohibited_operation")
    _reject_secret(value)
    return value


def _normalize_prohibitions(
    value: Any,
    *,
    add_mandatory: bool,
) -> tuple[str, ...]:
    raw = _items(value, "invalid_codex_prohibited_operations")
    if len(raw) > _MAX_PROHIBITED_OPERATIONS:
        _fail("invalid_codex_prohibited_operations")
    _reject_secret(raw)
    operations = {_normalize_operation(item) for item in raw}
    mandatory = set(CODEX_HANDOFF_MANDATORY_PROHIBITIONS)
    if add_mandatory:
        operations.update(mandatory)
    elif not mandatory.issubset(operations):
        _fail("codex_mandatory_prohibition_missing")
    if len(operations) > _MAX_PROHIBITED_OPERATIONS:
        _fail("invalid_codex_prohibited_operations")
    return tuple(sorted(operations))


@dataclass(frozen=True)
class CodexAdapterAuditSnapshot:
    """Immutable code-owned result of the 2026-08-01 local CLI audit."""

    adapter_mode: str = "handoff_only"
    executor_available: bool = False
    reason_code: str = "codex_cli_access_denied"
    desktop_package_version: str = "26.727.6591.0"
    cli_version: str | None = None
    supported_executable_path: str | None = None
    supported_noninteractive_entrypoint: str | None = None
    cwd_control: str = "unverified"
    sandbox_control: str = "unverified"
    approval_control: str = "unverified"
    command_allowlist_control: str = "unverified"
    structured_output_control: str = "unverified"
    log_control: str = "unverified"
    token_budget_control: str = "unverified"
    timeout_control: str = "unverified"
    process_protocol: str = "unverified"
    local_help_state: str = "access_denied"
    real_delegation_performed: bool = False
    real_acceptance_performed: bool = False
    contract_version: str = CODEX_AUDIT_CONTRACT_VERSION
    audit_digest: str = ""
    _factory_seal: InitVar[object | None] = None

    def __post_init__(self, _factory_seal: object | None) -> None:
        if _factory_seal is not _AUDIT_FACTORY_SEAL:
            _fail("codex_audit_snapshot_factory_required")
        fixed = {
            "adapter_mode": "handoff_only",
            "executor_available": False,
            "reason_code": "codex_cli_access_denied",
            "desktop_package_version": "26.727.6591.0",
            "cli_version": None,
            "supported_executable_path": None,
            "supported_noninteractive_entrypoint": None,
            "cwd_control": "unverified",
            "sandbox_control": "unverified",
            "approval_control": "unverified",
            "command_allowlist_control": "unverified",
            "structured_output_control": "unverified",
            "log_control": "unverified",
            "token_budget_control": "unverified",
            "timeout_control": "unverified",
            "process_protocol": "unverified",
            "local_help_state": "access_denied",
            "real_delegation_performed": False,
            "real_acceptance_performed": False,
            "contract_version": CODEX_AUDIT_CONTRACT_VERSION,
        }
        if any(getattr(self, key) != value for key, value in fixed.items()):
            _fail("codex_audit_snapshot_is_code_owned")
        expected = canonical_digest(self.identity_payload())
        if self.audit_digest:
            supplied = _digest(
                self.audit_digest,
                "invalid_codex_audit_digest",
            )
            if supplied != expected:
                _fail("codex_audit_digest_mismatch")
        object.__setattr__(self, "audit_digest", expected)

    def identity_payload(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "adapter_mode": self.adapter_mode,
            "executor_available": self.executor_available,
            "reason_code": self.reason_code,
            "desktop_package_version": self.desktop_package_version,
            "cli_version": self.cli_version,
            "supported_executable_path": self.supported_executable_path,
            "supported_noninteractive_entrypoint": (
                self.supported_noninteractive_entrypoint
            ),
            "cwd_control": self.cwd_control,
            "sandbox_control": self.sandbox_control,
            "approval_control": self.approval_control,
            "command_allowlist_control": self.command_allowlist_control,
            "structured_output_control": self.structured_output_control,
            "log_control": self.log_control,
            "token_budget_control": self.token_budget_control,
            "timeout_control": self.timeout_control,
            "process_protocol": self.process_protocol,
            "local_help_state": self.local_help_state,
            "real_delegation_performed": self.real_delegation_performed,
            "real_acceptance_performed": self.real_acceptance_performed,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_payload(), "audit_digest": self.audit_digest}


def build_code_owned_codex_audit_snapshot() -> CodexAdapterAuditSnapshot:
    """Return the fixed audit result; callers cannot supply its state."""

    return CodexAdapterAuditSnapshot(_factory_seal=_AUDIT_FACTORY_SEAL)


def validate_codex_audit_snapshot(
    value: Mapping[str, Any],
) -> CodexAdapterAuditSnapshot:
    """Accept only the exact current code-owned snapshot."""

    expected = build_code_owned_codex_audit_snapshot()
    if not isinstance(value, Mapping) or dict(value) != expected.to_dict():
        _fail("codex_audit_snapshot_mismatch")
    return expected


@dataclass(frozen=True, order=True)
class CodexHandoffContextReference:
    """A digest-only pointer; it contains no context body or model prompt."""

    context_kind: str
    context_digest: str
    _factory_seal: InitVar[object | None] = None

    def __post_init__(self, _factory_seal: object | None) -> None:
        if _factory_seal is not _CONTEXT_FACTORY_SEAL:
            _fail("codex_context_reference_factory_required")
        if self.context_kind not in CODEX_HANDOFF_CONTEXT_KINDS:
            _fail("invalid_codex_context_kind")
        object.__setattr__(
            self,
            "context_digest",
            _digest(self.context_digest, "invalid_codex_context_digest"),
        )
        _reject_secret(self.to_dict())

    def to_dict(self) -> dict[str, str]:
        return {
            "context_kind": self.context_kind,
            "context_digest": self.context_digest,
        }


def create_codex_handoff_context_reference(
    *,
    context_kind: str,
    context_digest: str,
) -> CodexHandoffContextReference:
    return CodexHandoffContextReference(
        context_kind=context_kind,
        context_digest=context_digest,
        _factory_seal=_CONTEXT_FACTORY_SEAL,
    )


def _normalize_context_references(
    value: Any,
) -> tuple[CodexHandoffContextReference, ...]:
    raw = _items(value, "invalid_codex_context_references")
    if len(raw) > _MAX_CONTEXT_REFERENCES:
        _fail("invalid_codex_context_references")
    materialized = [
        item.to_dict()
        if isinstance(item, CodexHandoffContextReference)
        else item
        for item in raw
    ]
    _reject_secret(materialized)
    references: list[CodexHandoffContextReference] = []
    for item in raw:
        if isinstance(item, CodexHandoffContextReference):
            reference = item
        elif isinstance(item, Mapping) and set(item) == {
            "context_kind",
            "context_digest",
        }:
            reference = create_codex_handoff_context_reference(
                context_kind=item["context_kind"],
                context_digest=item["context_digest"],
            )
        else:
            _fail("invalid_codex_context_reference")
        references.append(reference)
    if len(set(references)) != len(references):
        _fail("duplicate_codex_context_reference")
    return tuple(sorted(references))


@dataclass(frozen=True)
class CodexHandoffArtifact:
    """Digest-only handoff metadata that grants no execution authority."""

    objective_digest: str
    plan_digest: str
    delegation_digest: str
    context_references: tuple[CodexHandoffContextReference, ...]
    authorized_read_roots: tuple[str, ...]
    authorized_write_roots: tuple[str, ...]
    prohibited_operations: tuple[str, ...]
    adapter_mode: str = "handoff_only"
    execution_allowed: bool = False
    grants_authority: bool = False
    grants_acceptance: bool = False
    contract_version: str = CODEX_HANDOFF_CONTRACT_VERSION
    artifact_digest: str = ""
    _factory_seal: InitVar[object | None] = None

    def __post_init__(self, _factory_seal: object | None) -> None:
        if _factory_seal is not _HANDOFF_FACTORY_SEAL:
            _fail("codex_handoff_artifact_factory_required")
        if (
            self.adapter_mode != "handoff_only"
            or self.execution_allowed is not False
            or self.grants_authority is not False
            or self.grants_acceptance is not False
            or self.contract_version != CODEX_HANDOFF_CONTRACT_VERSION
        ):
            _fail("codex_handoff_scope_violation")
        for field_name in (
            "objective_digest",
            "plan_digest",
            "delegation_digest",
        ):
            object.__setattr__(
                self,
                field_name,
                _digest(
                    getattr(self, field_name),
                    f"invalid_codex_{field_name}",
                ),
            )
        if (
            not isinstance(self.context_references, tuple)
            or any(
                not isinstance(item, CodexHandoffContextReference)
                for item in self.context_references
            )
            or tuple(sorted(self.context_references)) != self.context_references
            or len(set(self.context_references)) != len(self.context_references)
        ):
            _fail("invalid_codex_context_references")
        if _normalize_roots(
            self.authorized_read_roots,
            required=True,
        ) != self.authorized_read_roots:
            _fail("noncanonical_codex_read_roots")
        if _normalize_roots(
            self.authorized_write_roots,
            required=False,
        ) != self.authorized_write_roots:
            _fail("noncanonical_codex_write_roots")
        _validate_write_roots(
            self.authorized_read_roots,
            self.authorized_write_roots,
        )
        if _normalize_prohibitions(
            self.prohibited_operations,
            add_mandatory=False,
        ) != self.prohibited_operations:
            _fail("noncanonical_codex_prohibited_operations")
        _reject_secret(self.identity_payload())
        expected = canonical_digest(self.identity_payload())
        if self.artifact_digest:
            supplied = _digest(
                self.artifact_digest,
                "invalid_codex_handoff_artifact_digest",
            )
            if supplied != expected:
                _fail("codex_handoff_artifact_digest_mismatch")
        object.__setattr__(self, "artifact_digest", expected)

    def identity_payload(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "adapter_mode": self.adapter_mode,
            "objective_digest": self.objective_digest,
            "plan_digest": self.plan_digest,
            "delegation_digest": self.delegation_digest,
            "context_references": [
                item.to_dict() for item in self.context_references
            ],
            "authorized_read_roots": list(self.authorized_read_roots),
            "authorized_write_roots": list(self.authorized_write_roots),
            "prohibited_operations": list(self.prohibited_operations),
            "execution_allowed": self.execution_allowed,
            "grants_authority": self.grants_authority,
            "grants_acceptance": self.grants_acceptance,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.identity_payload(),
            "artifact_digest": self.artifact_digest,
        }

    def verify_canonical_digest(self) -> str:
        expected = canonical_digest(self.identity_payload())
        if self.artifact_digest != expected:
            _fail("codex_handoff_artifact_digest_mismatch")
        return expected


def _build_codex_handoff_artifact(
    *,
    objective_digest: str,
    plan_digest: str,
    delegation_digest: str,
    context_references: Any,
    authorized_read_roots: Any,
    authorized_write_roots: Any,
    prohibited_operations: Any,
    supplied_artifact_digest: str = "",
) -> CodexHandoffArtifact:
    contexts = _normalize_context_references(context_references)
    read_roots = _normalize_roots(authorized_read_roots, required=True)
    write_roots = _normalize_roots(authorized_write_roots, required=False)
    _validate_write_roots(read_roots, write_roots)
    prohibitions = _normalize_prohibitions(
        prohibited_operations,
        add_mandatory=False,
    )
    return CodexHandoffArtifact(
        objective_digest=objective_digest,
        plan_digest=plan_digest,
        delegation_digest=delegation_digest,
        context_references=contexts,
        authorized_read_roots=read_roots,
        authorized_write_roots=write_roots,
        prohibited_operations=prohibitions,
        artifact_digest=supplied_artifact_digest,
        _factory_seal=_HANDOFF_FACTORY_SEAL,
    )


def create_codex_handoff_artifact(
    *,
    objective_digest: str,
    plan_digest: str,
    delegation_digest: str,
    context_references: Iterable[
        CodexHandoffContextReference | Mapping[str, Any]
    ] = (),
    authorized_read_roots: Iterable[str],
    authorized_write_roots: Iterable[str] = (),
    additional_prohibited_operations: Iterable[str] = (),
) -> CodexHandoffArtifact:
    """Create deterministic, digest-only metadata for a manual handoff.

    The factory deliberately accepts no prompt, command, environment, secret,
    model result, execution status, authority claim, or acceptance claim.
    """

    raw_contexts = _items(
        context_references,
        "invalid_codex_context_references",
    )
    raw_read_roots = _items(
        authorized_read_roots,
        "invalid_codex_authorized_roots",
    )
    raw_write_roots = _items(
        authorized_write_roots,
        "invalid_codex_authorized_roots",
    )
    raw_additional = _items(
        additional_prohibited_operations,
        "invalid_codex_prohibited_operations",
    )
    context_material = [
        item.to_dict()
        if isinstance(item, CodexHandoffContextReference)
        else item
        for item in raw_contexts
    ]
    _reject_secret(
        {
            "context_references": context_material,
            "authorized_read_roots": raw_read_roots,
            "authorized_write_roots": raw_write_roots,
            "additional_prohibited_operations": raw_additional,
        }
    )
    prohibitions = _normalize_prohibitions(
        raw_additional,
        add_mandatory=True,
    )
    return _build_codex_handoff_artifact(
        objective_digest=objective_digest,
        plan_digest=plan_digest,
        delegation_digest=delegation_digest,
        context_references=raw_contexts,
        authorized_read_roots=raw_read_roots,
        authorized_write_roots=raw_write_roots,
        prohibited_operations=prohibitions,
    )


def load_codex_handoff_artifact(
    value: Mapping[str, Any],
) -> CodexHandoffArtifact:
    """Validate a serialized artifact and its canonical digest."""

    _reject_secret(value)
    expected_fields = {
        "contract_version",
        "adapter_mode",
        "objective_digest",
        "plan_digest",
        "delegation_digest",
        "context_references",
        "authorized_read_roots",
        "authorized_write_roots",
        "prohibited_operations",
        "execution_allowed",
        "grants_authority",
        "grants_acceptance",
        "artifact_digest",
    }
    if not isinstance(value, Mapping) or set(value) != expected_fields:
        _fail("invalid_codex_handoff_record")
    if (
        value["contract_version"] != CODEX_HANDOFF_CONTRACT_VERSION
        or value["adapter_mode"] != "handoff_only"
        or value["execution_allowed"] is not False
        or value["grants_authority"] is not False
        or value["grants_acceptance"] is not False
        or not isinstance(value["context_references"], list)
        or not isinstance(value["authorized_read_roots"], list)
        or not isinstance(value["authorized_write_roots"], list)
        or not isinstance(value["prohibited_operations"], list)
    ):
        _fail("codex_handoff_scope_violation")
    return _build_codex_handoff_artifact(
        objective_digest=value["objective_digest"],
        plan_digest=value["plan_digest"],
        delegation_digest=value["delegation_digest"],
        context_references=value["context_references"],
        authorized_read_roots=value["authorized_read_roots"],
        authorized_write_roots=value["authorized_write_roots"],
        prohibited_operations=value["prohibited_operations"],
        supplied_artifact_digest=value["artifact_digest"],
    )


@dataclass(frozen=True)
class CodexHandoffOnlyAdapter:
    """Non-executable adapter facade backed by the code-owned audit."""

    audit_snapshot: CodexAdapterAuditSnapshot
    _factory_seal: InitVar[object | None] = None

    def __post_init__(self, _factory_seal: object | None) -> None:
        if _factory_seal is not _ADAPTER_FACTORY_SEAL:
            _fail("codex_adapter_factory_required")
        if self.audit_snapshot != build_code_owned_codex_audit_snapshot():
            _fail("codex_adapter_audit_binding_mismatch")

    @property
    def adapter_mode(self) -> str:
        return self.audit_snapshot.adapter_mode

    @property
    def executor_available(self) -> bool:
        return False

    def create_handoff(self, **kwargs: Any) -> CodexHandoffArtifact:
        return create_codex_handoff_artifact(**kwargs)

    def execute(self, *_args: Any, **_kwargs: Any) -> None:
        """Fail closed; this local audit exposes no executable interface."""

        _fail("codex_execute_unavailable_handoff_only")


def create_codex_handoff_only_adapter() -> CodexHandoffOnlyAdapter:
    return CodexHandoffOnlyAdapter(
        audit_snapshot=build_code_owned_codex_audit_snapshot(),
        _factory_seal=_ADAPTER_FACTORY_SEAL,
    )


__all__ = [
    "CODEX_AUDIT_CONTRACT_VERSION",
    "CODEX_HANDOFF_CONTEXT_KINDS",
    "CODEX_HANDOFF_CONTRACT_VERSION",
    "CODEX_HANDOFF_MANDATORY_PROHIBITIONS",
    "CodexAdapterAuditSnapshot",
    "CodexAdapterError",
    "CodexHandoffArtifact",
    "CodexHandoffContextReference",
    "CodexHandoffOnlyAdapter",
    "build_code_owned_codex_audit_snapshot",
    "create_codex_handoff_artifact",
    "create_codex_handoff_context_reference",
    "create_codex_handoff_only_adapter",
    "load_codex_handoff_artifact",
    "validate_codex_audit_snapshot",
]
