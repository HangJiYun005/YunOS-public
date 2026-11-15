from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = 9001
PROJECTION_VERSION = 1
CONTRACT_VERSION = "yunos-operational-state-core-1"

ENTITY_TYPES = frozenset(
    {
        "project",
        "goal",
        "work_item",
        "decision",
        "commitment",
        "blocker",
        "resource",
        "delegation",
        "outcome",
    }
)
SOURCE_KINDS = frozenset(
    {"declared", "observed", "inferred_candidate", "accepted_outcome"}
)
AUTHORITY_KINDS = frozenset(
    {
        "user",
        "deterministic_observation",
        "verified_runtime",
        "accepted_outcome",
        "candidate_only",
        "system_reconciliation",
    }
)
ACTOR_KINDS = frozenset({"user", "agent", "system", "runtime", "test"})
PRIVACY_SCOPES = frozenset(
    {"public", "private_metadata", "private_content", "secret_reference"}
)
FRESHNESS_STATES = frozenset(
    {
        "fresh",
        "aging",
        "stale",
        "expired",
        "superseded",
        "conflicted",
        "unverifiable",
    }
)

LIFECYCLES: dict[str, dict[str, frozenset[str]]] = {
    "project": {
        "proposed": frozenset({"active", "archived"}),
        "active": frozenset({"paused", "completed"}),
        "paused": frozenset({"active", "archived"}),
        "completed": frozenset({"archived"}),
        "archived": frozenset(),
    },
    "goal": {
        "candidate": frozenset({"approved", "abandoned", "superseded"}),
        "approved": frozenset({"active", "abandoned", "superseded"}),
        "active": frozenset({"paused", "achieved", "abandoned", "superseded"}),
        "paused": frozenset({"active", "abandoned", "superseded"}),
        "achieved": frozenset({"superseded"}),
        "abandoned": frozenset(),
        "superseded": frozenset(),
    },
    "work_item": {
        "proposed": frozenset({"ready", "cancelled", "superseded"}),
        "ready": frozenset({"in_progress", "cancelled", "superseded"}),
        "in_progress": frozenset(
            {"blocked", "waiting", "completed", "cancelled", "superseded"}
        ),
        "blocked": frozenset(
            {"in_progress", "waiting", "cancelled", "superseded"}
        ),
        "waiting": frozenset(
            {"in_progress", "blocked", "cancelled", "superseded"}
        ),
        "completed": frozenset({"superseded"}),
        "cancelled": frozenset(),
        "superseded": frozenset(),
    },
    "decision": {
        "candidate": frozenset({"approved", "rejected"}),
        "approved": frozenset({"superseded", "revoked"}),
        "rejected": frozenset(),
        "superseded": frozenset(),
        "revoked": frozenset(),
    },
    "commitment": {
        "active": frozenset({"fulfilled", "cancelled", "expired", "breached"}),
        "fulfilled": frozenset(),
        "cancelled": frozenset(),
        "expired": frozenset(),
        "breached": frozenset(),
    },
    "blocker": {
        "open": frozenset({"mitigated", "resolved", "obsolete"}),
        "mitigated": frozenset({"open", "resolved", "obsolete"}),
        "resolved": frozenset(),
        "obsolete": frozenset(),
    },
    "delegation": {
        "proposed": frozenset({"authorized", "cancelled"}),
        "authorized": frozenset({"queued", "waiting", "cancelled"}),
        "queued": frozenset({"running", "waiting", "cancelled"}),
        "running": frozenset({"waiting", "completed", "failed", "cancelled"}),
        "waiting": frozenset({"running", "completed", "failed", "cancelled"}),
        "completed": frozenset({"accepted", "rejected"}),
        "failed": frozenset({"rejected"}),
        "cancelled": frozenset(),
        "accepted": frozenset(),
        "rejected": frozenset(),
    },
    "outcome": {
        "reported": frozenset({"verified", "rejected", "superseded"}),
        "verified": frozenset({"accepted", "rejected", "superseded"}),
        "accepted": frozenset({"superseded"}),
        "rejected": frozenset({"superseded"}),
        "superseded": frozenset(),
    },
    "resource": {
        "available": frozenset({"unavailable", "stale"}),
        "unavailable": frozenset({"available", "stale"}),
        "stale": frozenset({"available", "unavailable"}),
    },
}

INITIAL_STATES: dict[str, frozenset[str]] = {
    "project": frozenset({"proposed", "active", "paused", "completed", "archived"}),
    "goal": frozenset({"candidate", "approved", "active", "paused", "achieved"}),
    "work_item": frozenset({"proposed", "ready", "in_progress", "blocked", "waiting"}),
    "decision": frozenset({"candidate", "approved", "rejected"}),
    "commitment": frozenset({"active"}),
    "blocker": frozenset({"open", "mitigated"}),
    "delegation": frozenset({"proposed", "authorized"}),
    "outcome": frozenset({"reported"}),
    "resource": frozenset({"available", "unavailable", "stale"}),
}

_IDENTITY_RE = re.compile(r"^[a-z][a-z0-9_.:-]{2,191}$")
_EVENT_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_AUTHORITY_RANK = {
    "candidate_only": 0,
    "system_reconciliation": 20,
    "deterministic_observation": 30,
    "verified_runtime": 35,
    "accepted_outcome": 40,
    "user": 50,
}


class OperationalStateError(RuntimeError):
    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


def _normalize_json(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise OperationalStateError("non_finite_number")
        return value
    if isinstance(value, str):
        normalized = unicodedata.normalize("NFC", value)
        if "\x00" in normalized:
            raise OperationalStateError("nul_not_allowed")
        return normalized
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise OperationalStateError("json_object_key_not_string")
            normalized_key = unicodedata.normalize("NFC", key)
            if normalized_key in normalized:
                raise OperationalStateError("duplicate_normalized_json_key")
            normalized[normalized_key] = _normalize_json(item)
        return normalized
    if isinstance(value, (list, tuple)):
        return [_normalize_json(item) for item in value]
    raise OperationalStateError("unsupported_json_value")


def canonical_json(value: Any) -> str:
    return json.dumps(
        _normalize_json(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def normalize_timestamp(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise OperationalStateError("invalid_timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise OperationalStateError("invalid_timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise OperationalStateError("timestamp_must_be_utc")
    return parsed.astimezone(timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def validate_identity(value: str, reason_code: str = "invalid_identity") -> str:
    if not isinstance(value, str) or _IDENTITY_RE.fullmatch(value) is None:
        raise OperationalStateError(reason_code)
    return value


def validate_sha256(value: str, reason_code: str = "invalid_sha256") -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise OperationalStateError(reason_code)
    return value


def make_identity(prefix: str, value: Any) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9_-]{1,31}", prefix):
        raise OperationalStateError("invalid_identity_prefix")
    return f"{prefix}:sha256:{canonical_digest(value)}"


def authority_rank(authority_kind: str) -> int:
    try:
        return _AUTHORITY_RANK[authority_kind]
    except KeyError:
        raise OperationalStateError("invalid_authority_kind") from None


def validate_transition(
    entity_type: str,
    previous_state: str | None,
    next_state: str,
) -> None:
    if entity_type not in ENTITY_TYPES:
        raise OperationalStateError("invalid_entity_type")
    lifecycle = LIFECYCLES[entity_type]
    if next_state not in lifecycle:
        raise OperationalStateError("invalid_entity_state")
    if previous_state is None:
        if next_state not in INITIAL_STATES[entity_type]:
            raise OperationalStateError("invalid_initial_state")
        return
    if previous_state == next_state:
        return
    if next_state not in lifecycle[previous_state]:
        raise OperationalStateError("invalid_state_transition")


@dataclass(frozen=True)
class SourceAnchor:
    anchor_id: str
    source_kind: str
    locator: str
    evidence_digest: str
    observed_at: str
    privacy_scope: str

    def __post_init__(self) -> None:
        validate_identity(self.anchor_id, "invalid_anchor_id")
        if self.source_kind not in {
            "user_declaration",
            "git",
            "database",
            "file",
            "service",
            "runtime",
            "approved_memory",
            "test",
        }:
            raise OperationalStateError("invalid_anchor_source_kind")
        if not isinstance(self.locator, str) or not 1 <= len(self.locator) <= 512:
            raise OperationalStateError("invalid_anchor_locator")
        validate_sha256(self.evidence_digest, "invalid_anchor_evidence_digest")
        object.__setattr__(self, "observed_at", normalize_timestamp(self.observed_at))
        if self.privacy_scope not in PRIVACY_SCOPES:
            raise OperationalStateError("invalid_privacy_scope")

    def to_dict(self) -> dict[str, Any]:
        return {
            "anchor_id": self.anchor_id,
            "source_kind": self.source_kind,
            "locator": self.locator,
            "evidence_digest": self.evidence_digest,
            "observed_at": self.observed_at,
            "privacy_scope": self.privacy_scope,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SourceAnchor":
        expected = {
            "anchor_id",
            "source_kind",
            "locator",
            "evidence_digest",
            "observed_at",
            "privacy_scope",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise OperationalStateError("invalid_source_anchor")
        return cls(**dict(value))


@dataclass(frozen=True)
class AuthorityArtifact:
    authority_id: str
    issued_by: str
    issued_at: str
    expires_at: str
    target_project_id: str
    allowed_event_types: tuple[str, ...]
    read_scope: tuple[str, ...]
    write_scope: tuple[str, ...]
    prohibited_operations: tuple[str, ...]
    commit_permission: bool = False
    merge_permission: bool = False
    tag_permission: bool = False
    push_permission: bool = False
    next_node_permission: bool = False
    authority_digest: str = ""

    def __post_init__(self) -> None:
        validate_identity(self.authority_id, "invalid_authority_id")
        validate_identity(self.issued_by, "invalid_authority_issuer")
        object.__setattr__(self, "issued_at", normalize_timestamp(self.issued_at))
        object.__setattr__(self, "expires_at", normalize_timestamp(self.expires_at))
        if self.expires_at <= self.issued_at:
            raise OperationalStateError("invalid_authority_interval")
        validate_identity(self.target_project_id, "invalid_authority_project")
        if not self.allowed_event_types:
            raise OperationalStateError("authority_event_types_required")
        for event_type in self.allowed_event_types:
            if not isinstance(event_type, str) or _EVENT_TYPE_RE.fullmatch(event_type) is None:
                raise OperationalStateError("invalid_authority_event_type")
        for values in (
            self.read_scope,
            self.write_scope,
            self.prohibited_operations,
        ):
            if any(not isinstance(item, str) or not item for item in values):
                raise OperationalStateError("invalid_authority_scope")
        expected = canonical_digest(self.identity_payload())
        if self.authority_digest and self.authority_digest != expected:
            raise OperationalStateError("authority_digest_mismatch")
        object.__setattr__(self, "authority_digest", expected)

    def identity_payload(self) -> dict[str, Any]:
        return {
            "authority_id": self.authority_id,
            "issued_by": self.issued_by,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "target_project_id": self.target_project_id,
            "allowed_event_types": sorted(set(self.allowed_event_types)),
            "read_scope": sorted(set(self.read_scope)),
            "write_scope": sorted(set(self.write_scope)),
            "prohibited_operations": sorted(set(self.prohibited_operations)),
            "commit_permission": self.commit_permission,
            "merge_permission": self.merge_permission,
            "tag_permission": self.tag_permission,
            "push_permission": self.push_permission,
            "next_node_permission": self.next_node_permission,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_payload(), "authority_digest": self.authority_digest}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AuthorityArtifact":
        expected = {
            "authority_id",
            "issued_by",
            "issued_at",
            "expires_at",
            "target_project_id",
            "allowed_event_types",
            "read_scope",
            "write_scope",
            "prohibited_operations",
            "commit_permission",
            "merge_permission",
            "tag_permission",
            "push_permission",
            "next_node_permission",
            "authority_digest",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise OperationalStateError("invalid_authority_artifact")
        payload = dict(value)
        for field in (
            "allowed_event_types",
            "read_scope",
            "write_scope",
            "prohibited_operations",
        ):
            if not isinstance(payload[field], list):
                raise OperationalStateError("invalid_authority_artifact")
            payload[field] = tuple(payload[field])
        return cls(**payload)

    def authorize_event(
        self,
        *,
        project_id: str,
        entity_id: str,
        event_type: str,
        at: str,
    ) -> None:
        current = normalize_timestamp(at)
        if not (self.issued_at <= current < self.expires_at):
            raise OperationalStateError("authority_expired_or_not_yet_valid")
        if project_id != self.target_project_id:
            raise OperationalStateError("authority_project_mismatch")
        if event_type not in self.allowed_event_types:
            raise OperationalStateError("event_type_not_authorized")
        if not self.commit_permission:
            raise OperationalStateError("event_commit_not_authorized")
        if entity_id not in self.write_scope and "*" not in self.write_scope:
            raise OperationalStateError("entity_write_not_authorized")


@dataclass(frozen=True)
class StateEvent:
    event_id: str
    project_id: str
    entity_id: str
    entity_type: str
    event_type: str
    source_kind: str
    source_anchor: SourceAnchor
    authority_kind: str
    actor_kind: str
    actor_id: str
    observed_at: str
    effective_at: str
    recorded_at: str
    expires_at: str | None
    supersedes_event_id: str | None
    caused_by_event_id: str | None
    idempotency_key: str
    conflict_group_id: str | None
    canonical_payload: dict[str, Any]
    payload_digest: str
    privacy_scope: str
    projection_version: int = PROJECTION_VERSION
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        validate_identity(self.event_id, "invalid_event_id")
        validate_identity(self.project_id, "invalid_project_id")
        validate_identity(self.entity_id, "invalid_entity_id")
        if self.entity_type not in ENTITY_TYPES:
            raise OperationalStateError("invalid_entity_type")
        if not isinstance(self.event_type, str) or _EVENT_TYPE_RE.fullmatch(
            self.event_type
        ) is None:
            raise OperationalStateError("invalid_event_type")
        if self.source_kind not in SOURCE_KINDS:
            raise OperationalStateError("invalid_source_kind")
        if self.authority_kind not in AUTHORITY_KINDS:
            raise OperationalStateError("invalid_authority_kind")
        if self.actor_kind not in ACTOR_KINDS:
            raise OperationalStateError("invalid_actor_kind")
        validate_identity(self.actor_id, "invalid_actor_id")
        for field in ("observed_at", "effective_at", "recorded_at"):
            object.__setattr__(self, field, normalize_timestamp(getattr(self, field)))
        if self.expires_at is not None:
            object.__setattr__(
                self, "expires_at", normalize_timestamp(self.expires_at)
            )
        for field in (
            "supersedes_event_id",
            "caused_by_event_id",
            "conflict_group_id",
        ):
            value = getattr(self, field)
            if value is not None:
                validate_identity(value, f"invalid_{field}")
        validate_identity(self.idempotency_key, "invalid_idempotency_key")
        normalized = _normalize_json(self.canonical_payload)
        if not isinstance(normalized, dict):
            raise OperationalStateError("event_payload_must_be_object")
        object.__setattr__(self, "canonical_payload", normalized)
        expected = canonical_digest(normalized)
        if self.payload_digest != expected:
            raise OperationalStateError("payload_digest_mismatch")
        if self.privacy_scope not in PRIVACY_SCOPES:
            raise OperationalStateError("invalid_privacy_scope")
        if self.projection_version != PROJECTION_VERSION:
            raise OperationalStateError("projection_version_mismatch")
        if self.schema_version != SCHEMA_VERSION:
            raise OperationalStateError("schema_version_mismatch")
        if self.source_kind == "inferred_candidate":
            raise OperationalStateError("candidate_cannot_be_authoritative_event")
        if self.authority_kind == "candidate_only":
            raise OperationalStateError("candidate_authority_cannot_project")
        if self.canonical_payload.get("state") not in LIFECYCLES[self.entity_type]:
            raise OperationalStateError("event_state_required")

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "project_id": self.project_id,
            "entity_id": self.entity_id,
            "entity_type": self.entity_type,
            "event_type": self.event_type,
            "source_kind": self.source_kind,
            "source_anchor": self.source_anchor.to_dict(),
            "authority_kind": self.authority_kind,
            "actor_kind": self.actor_kind,
            "actor_id": self.actor_id,
            "observed_at": self.observed_at,
            "effective_at": self.effective_at,
            "recorded_at": self.recorded_at,
            "expires_at": self.expires_at,
            "supersedes_event_id": self.supersedes_event_id,
            "caused_by_event_id": self.caused_by_event_id,
            "idempotency_key": self.idempotency_key,
            "conflict_group_id": self.conflict_group_id,
            "canonical_payload": self.canonical_payload,
            "payload_digest": self.payload_digest,
            "privacy_scope": self.privacy_scope,
            "projection_version": self.projection_version,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "StateEvent":
        expected = {
            "event_id",
            "project_id",
            "entity_id",
            "entity_type",
            "event_type",
            "source_kind",
            "source_anchor",
            "authority_kind",
            "actor_kind",
            "actor_id",
            "observed_at",
            "effective_at",
            "recorded_at",
            "expires_at",
            "supersedes_event_id",
            "caused_by_event_id",
            "idempotency_key",
            "conflict_group_id",
            "canonical_payload",
            "payload_digest",
            "privacy_scope",
            "projection_version",
            "schema_version",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise OperationalStateError("invalid_state_event")
        payload = dict(value)
        payload["source_anchor"] = SourceAnchor.from_dict(payload["source_anchor"])
        return cls(**payload)

    def expected_event_id(self, authority_digest: str) -> str:
        identity_payload = {
            "project_id": self.project_id,
            "entity_id": self.entity_id,
            "entity_type": self.entity_type,
            "event_type": self.event_type,
            "source_kind": self.source_kind,
            "source_anchor_id": self.source_anchor.anchor_id,
            "authority_kind": self.authority_kind,
            "actor_kind": self.actor_kind,
            "actor_id": self.actor_id,
            "observed_at": self.observed_at,
            "effective_at": self.effective_at,
            "expires_at": self.expires_at,
            "supersedes_event_id": self.supersedes_event_id,
            "caused_by_event_id": self.caused_by_event_id,
            "idempotency_key": self.idempotency_key,
            "conflict_group_id": self.conflict_group_id,
            "payload_digest": self.payload_digest,
            "privacy_scope": self.privacy_scope,
            "authority_digest": validate_sha256(
                authority_digest, "invalid_authority_digest"
            ),
        }
        return make_identity("event", identity_payload)

    def validate_event_id(self, authority_digest: str) -> None:
        if self.event_id != self.expected_event_id(authority_digest):
            raise OperationalStateError("event_id_mismatch")


def build_state_event(
    *,
    project_id: str,
    entity_id: str,
    entity_type: str,
    event_type: str,
    source_kind: str,
    source_anchor: SourceAnchor,
    authority_kind: str,
    actor_kind: str,
    actor_id: str,
    observed_at: str,
    effective_at: str,
    recorded_at: str,
    expires_at: str | None,
    supersedes_event_id: str | None,
    caused_by_event_id: str | None,
    idempotency_key: str,
    conflict_group_id: str | None,
    payload: Mapping[str, Any],
    privacy_scope: str,
    authority_digest: str,
) -> StateEvent:
    canonical_payload = _normalize_json(payload)
    payload_digest = canonical_digest(canonical_payload)
    identity_payload = {
        "project_id": project_id,
        "entity_id": entity_id,
        "entity_type": entity_type,
        "event_type": event_type,
        "source_kind": source_kind,
        "source_anchor_id": source_anchor.anchor_id,
        "authority_kind": authority_kind,
        "actor_kind": actor_kind,
        "actor_id": actor_id,
        "observed_at": normalize_timestamp(observed_at),
        "effective_at": normalize_timestamp(effective_at),
        "expires_at": normalize_timestamp(expires_at) if expires_at else None,
        "supersedes_event_id": supersedes_event_id,
        "caused_by_event_id": caused_by_event_id,
        "idempotency_key": idempotency_key,
        "conflict_group_id": conflict_group_id,
        "payload_digest": payload_digest,
        "privacy_scope": privacy_scope,
        "authority_digest": validate_sha256(
            authority_digest, "invalid_authority_digest"
        ),
    }
    return StateEvent(
        event_id=make_identity("event", identity_payload),
        project_id=project_id,
        entity_id=entity_id,
        entity_type=entity_type,
        event_type=event_type,
        source_kind=source_kind,
        source_anchor=source_anchor,
        authority_kind=authority_kind,
        actor_kind=actor_kind,
        actor_id=actor_id,
        observed_at=observed_at,
        effective_at=effective_at,
        recorded_at=recorded_at,
        expires_at=expires_at,
        supersedes_event_id=supersedes_event_id,
        caused_by_event_id=caused_by_event_id,
        idempotency_key=idempotency_key,
        conflict_group_id=conflict_group_id,
        canonical_payload=canonical_payload,
        payload_digest=payload_digest,
        privacy_scope=privacy_scope,
    )


@dataclass(frozen=True)
class AppendPreview:
    event: StateEvent
    authority_digest: str
    previous_projection_digest: str | None
    preview_digest: str

    def __post_init__(self) -> None:
        validate_sha256(self.authority_digest, "invalid_authority_digest")
        if self.previous_projection_digest is not None:
            validate_sha256(
                self.previous_projection_digest,
                "invalid_previous_projection_digest",
            )
        expected = canonical_digest(self.identity_payload())
        if self.preview_digest != expected:
            raise OperationalStateError("preview_digest_mismatch")

    def identity_payload(self) -> dict[str, Any]:
        return {
            "event": self.event.to_dict(),
            "authority_digest": self.authority_digest,
            "previous_projection_digest": self.previous_projection_digest,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_payload(), "preview_digest": self.preview_digest}

    @classmethod
    def create(
        cls,
        event: StateEvent,
        authority_digest: str,
        previous_projection_digest: str | None,
    ) -> "AppendPreview":
        payload = {
            "event": event.to_dict(),
            "authority_digest": authority_digest,
            "previous_projection_digest": previous_projection_digest,
        }
        return cls(
            event=event,
            authority_digest=authority_digest,
            previous_projection_digest=previous_projection_digest,
            preview_digest=canonical_digest(payload),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AppendPreview":
        if not isinstance(value, Mapping) or set(value) != {
            "event",
            "authority_digest",
            "previous_projection_digest",
            "preview_digest",
        }:
            raise OperationalStateError("invalid_append_preview")
        return cls(
            event=StateEvent.from_dict(value["event"]),
            authority_digest=value["authority_digest"],
            previous_projection_digest=value["previous_projection_digest"],
            preview_digest=value["preview_digest"],
        )


@dataclass(frozen=True)
class AppendReceipt:
    receipt_id: str
    event_id: str
    idempotency_key: str
    authority_digest: str
    preview_digest: str
    projection_digest: str
    sequence: int
    recorded_at: str
    idempotent_replay: bool
    receipt_digest: str

    def identity_payload(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "event_id": self.event_id,
            "idempotency_key": self.idempotency_key,
            "authority_digest": self.authority_digest,
            "preview_digest": self.preview_digest,
            "projection_digest": self.projection_digest,
            "sequence": self.sequence,
            "recorded_at": self.recorded_at,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.identity_payload(),
            "idempotent_replay": self.idempotent_replay,
            "receipt_digest": self.receipt_digest,
        }

    @classmethod
    def create(
        cls,
        *,
        event: StateEvent,
        authority_digest: str,
        preview_digest: str,
        projection_digest: str,
        sequence: int,
    ) -> "AppendReceipt":
        receipt_id = make_identity(
            "receipt",
            {
                "event_id": event.event_id,
                "idempotency_key": event.idempotency_key,
                "preview_digest": preview_digest,
            },
        )
        payload = {
            "receipt_id": receipt_id,
            "event_id": event.event_id,
            "idempotency_key": event.idempotency_key,
            "authority_digest": authority_digest,
            "preview_digest": preview_digest,
            "projection_digest": projection_digest,
            "sequence": sequence,
            "recorded_at": event.recorded_at,
        }
        return cls(
            **payload,
            idempotent_replay=False,
            receipt_digest=canonical_digest(payload),
        )

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any], *, idempotent_replay: bool = False
    ) -> "AppendReceipt":
        expected = {
            "receipt_id",
            "event_id",
            "idempotency_key",
            "authority_digest",
            "preview_digest",
            "projection_digest",
            "sequence",
            "recorded_at",
            "receipt_digest",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise OperationalStateError("invalid_append_receipt")
        payload = dict(value)
        receipt_digest = payload.pop("receipt_digest")
        if canonical_digest(payload) != receipt_digest:
            raise OperationalStateError("receipt_digest_mismatch")
        return cls(
            **payload,
            idempotent_replay=idempotent_replay,
            receipt_digest=receipt_digest,
        )


def projection_sort_key(event: StateEvent) -> tuple[int, str, str, str]:
    return (
        authority_rank(event.authority_kind),
        event.effective_at,
        event.recorded_at,
        event.event_id,
    )


def build_projection(
    events: Sequence[tuple[int, StateEvent]],
    *,
    freshness: str = "fresh",
) -> dict[str, Any]:
    if not events:
        raise OperationalStateError("projection_requires_events")
    if freshness not in FRESHNESS_STATES:
        raise OperationalStateError("invalid_freshness")
    entity_ids = {event.entity_id for _, event in events}
    entity_types = {event.entity_type for _, event in events}
    project_ids = {event.project_id for _, event in events}
    if len(entity_ids) != 1 or len(entity_types) != 1 or len(project_ids) != 1:
        raise OperationalStateError("projection_entity_mismatch")

    winners: dict[str, tuple[tuple[int, str, str, str], StateEvent]] = {}
    superseded = {
        event.supersedes_event_id
        for _, event in events
        if event.supersedes_event_id is not None
    }
    active_events = [item for item in events if item[1].event_id not in superseded]
    for _, event in active_events:
        rank = projection_sort_key(event)
        for field in sorted(event.canonical_payload):
            if field.startswith("_"):
                continue
            current = winners.get(field)
            if current is None or rank > current[0]:
                winners[field] = (rank, event)

    fields = {
        field: event.canonical_payload[field]
        for field, (_, event) in sorted(winners.items())
    }
    winner_events = {
        field: event.event_id for field, (_, event) in sorted(winners.items())
    }
    state = fields.get("state")
    if not isinstance(state, str):
        raise OperationalStateError("projection_state_missing")
    last_sequence = max(sequence for sequence, _ in events)
    projection = {
        "contract_version": CONTRACT_VERSION,
        "project_id": next(iter(project_ids)),
        "entity_id": next(iter(entity_ids)),
        "entity_type": next(iter(entity_types)),
        "state": state,
        "freshness": freshness,
        "fields": fields,
        "winner_events": winner_events,
        "event_count": len(events),
        "active_event_count": len(active_events),
        "last_sequence": last_sequence,
        "projection_version": PROJECTION_VERSION,
        "schema_version": SCHEMA_VERSION,
    }
    projection["projection_digest"] = canonical_digest(projection)
    return projection
