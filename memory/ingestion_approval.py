"""Exact-plan approval artifacts for YunOS v8.3-E.

Approval is a pure, path-free decision model.  This module performs no file,
database, network, or production operations.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Any, Mapping, Sequence

from memory.ingestion_core import make_plan_digest, normalized_json_digest
from memory.ingestion_assets import AssetStagePlan
from memory.ingestion_planning import ImportConflict, ImportPlan


APPROVAL_SCHEMA_VERSION = "v8.3-e-approval-1"
CONFLICT_DECISION_SCHEMA_VERSION = "v8.3-e-conflict-decision-1"

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_IDENTITY_RE = re.compile(r"[a-z][a-z0-9_.:-]{7,191}\Z")
_TOKEN_RE = re.compile(r"[a-z][a-z0-9_.-]{0,95}\Z")

APPROVAL_SCOPES = frozenset({"exact_plan", "verification_only", "rollback_only"})
HIGH_RISK_LEVELS = frozenset({"high", "critical"})


class IngestionApprovalError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def _sha(value: str, reason: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise IngestionApprovalError(reason)
    return value


def _identity(value: str, reason: str) -> str:
    if not isinstance(value, str) or _IDENTITY_RE.fullmatch(value) is None:
        raise IngestionApprovalError(reason)
    return value


def _token(value: str, reason: str) -> str:
    if not isinstance(value, str) or _TOKEN_RE.fullmatch(value) is None:
        raise IngestionApprovalError(reason)
    return value


def _timestamp(value: str, reason: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise IngestionApprovalError(reason)
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise IngestionApprovalError(reason) from None
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise IngestionApprovalError(reason)
    return parsed


def _approval_plan_compatible(plan: Any) -> bool:
    """Return whether *plan* may use the unified exact-plan validator.

    v8.3-G historical backfill deliberately reuses ``ApprovalArtifact`` rather
    than introducing a second approval authority.  The historical plan exposes
    the same immutable approval-facing fields as ``ImportPlan`` and opts in
    with a fixed protocol marker.  Duck typing is kept deliberately narrow so
    arbitrary objects cannot bypass the v8.3-E validator.
    """

    if isinstance(plan, ImportPlan):
        return True
    return (
        getattr(plan, "approval_plan_kind", None) == "historical_backfill"
        and isinstance(getattr(plan, "batch_id", None), str)
        and isinstance(getattr(plan, "manifest_id", None), str)
        and isinstance(getattr(plan, "plan_id", None), str)
        and isinstance(getattr(plan, "conflicts", None), tuple)
        and callable(getattr(plan, "identity_payload", None))
    )


def import_plan_digest(plan: Any) -> str:
    if not _approval_plan_compatible(plan):
        raise IngestionApprovalError("invalid_import_plan")
    digest = (
        normalized_json_digest(plan.identity_payload())
        if isinstance(plan, ImportPlan)
        else make_plan_digest(plan.identity_payload())
    )
    declared = getattr(plan, "plan_digest", digest)
    if declared != digest:
        raise IngestionApprovalError("approval_plan_digest_mismatch")
    return digest


@dataclass(frozen=True)
class ConflictDecision:
    conflict_id: str
    conflict_type: str
    selected_action: str
    allowed_operation_ids: tuple[str, ...]
    rejected_operation_ids: tuple[str, ...]
    evidence_digest: str
    decision_reason_code: str
    actor_kind: str
    decision_schema_version: str = CONFLICT_DECISION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _identity(self.conflict_id, "invalid_conflict_id")
        _token(self.conflict_type, "invalid_conflict_type")
        _token(self.selected_action, "invalid_selected_action")
        allowed = tuple(sorted(set(self.allowed_operation_ids)))
        rejected = tuple(sorted(set(self.rejected_operation_ids)))
        for operation_id in (*allowed, *rejected):
            _identity(operation_id, "invalid_operation_id")
        if set(allowed).intersection(rejected):
            raise IngestionApprovalError("conflict_operation_overlap")
        object.__setattr__(self, "allowed_operation_ids", allowed)
        object.__setattr__(self, "rejected_operation_ids", rejected)
        _sha(self.evidence_digest, "invalid_evidence_digest")
        _token(self.decision_reason_code, "invalid_decision_reason_code")
        _token(self.actor_kind, "invalid_actor_kind")
        if self.decision_schema_version != CONFLICT_DECISION_SCHEMA_VERSION:
            raise IngestionApprovalError("decision_schema_version_mismatch")

    def to_dict(self) -> dict[str, Any]:
        return {
            "conflict_id": self.conflict_id,
            "conflict_type": self.conflict_type,
            "selected_action": self.selected_action,
            "allowed_operation_ids": list(self.allowed_operation_ids),
            "rejected_operation_ids": list(self.rejected_operation_ids),
            "evidence_digest": self.evidence_digest,
            "decision_reason_code": self.decision_reason_code,
            "actor_kind": self.actor_kind,
            "decision_schema_version": self.decision_schema_version,
        }


def make_conflict_decision(
    *,
    conflict: Any,
    selected_action: str,
    allowed_operation_ids: Sequence[str],
    rejected_operation_ids: Sequence[str],
    decision_reason_code: str,
    actor_kind: str,
) -> ConflictDecision:
    if not (
        isinstance(conflict, ImportConflict)
        or (
            getattr(conflict, "conflict_schema_version", None)
            == "v8.3-g-historical-conflict-1"
            and isinstance(getattr(conflict, "allowed_decisions", None), tuple)
            and isinstance(getattr(conflict, "evidence_hashes", None), tuple)
        )
    ):
        raise IngestionApprovalError("invalid_conflict")
    if selected_action not in conflict.allowed_decisions:
        raise IngestionApprovalError("conflict_action_not_proposed")
    evidence_digest = normalized_json_digest({
        "conflict_id": conflict.conflict_id,
        "conflict_type": conflict.conflict_type,
        "risk_level": conflict.risk_level,
        "evidence_hashes": list(conflict.evidence_hashes),
        "selected_action": selected_action,
    })
    return ConflictDecision(
        conflict_id=conflict.conflict_id,
        conflict_type=conflict.conflict_type,
        selected_action=selected_action,
        allowed_operation_ids=tuple(allowed_operation_ids),
        rejected_operation_ids=tuple(rejected_operation_ids),
        evidence_digest=evidence_digest,
        decision_reason_code=decision_reason_code,
        actor_kind=actor_kind,
    )


@dataclass(frozen=True)
class ApprovalArtifact:
    approval_id: str
    batch_id: str
    manifest_id: str
    plan_id: str
    plan_digest: str
    stage_plan_id: str
    stage_plan_digest: str
    target_snapshot_fingerprint: str
    conflict_decisions: tuple[ConflictDecision, ...]
    approved_operation_ids: tuple[str, ...]
    rejected_operation_ids: tuple[str, ...]
    approval_scope: str
    actor_kind: str
    created_at: str
    expires_at: str
    approval_digest: str
    approval_schema_version: str = APPROVAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _identity(self.approval_id, "invalid_approval_id")
        _identity(self.batch_id, "invalid_batch_id")
        _identity(self.manifest_id, "invalid_manifest_id")
        _identity(self.plan_id, "invalid_plan_id")
        _sha(self.plan_digest, "invalid_plan_digest")
        _identity(self.stage_plan_id, "invalid_stage_plan_id")
        _sha(self.stage_plan_digest, "invalid_stage_plan_digest")
        _sha(
            self.target_snapshot_fingerprint,
            "invalid_target_snapshot_fingerprint",
        )
        decisions = tuple(
            sorted(self.conflict_decisions, key=lambda item: item.conflict_id)
        )
        if len({item.conflict_id for item in decisions}) != len(decisions):
            raise IngestionApprovalError("duplicate_conflict_decision")
        object.__setattr__(self, "conflict_decisions", decisions)
        approved = tuple(sorted(set(self.approved_operation_ids)))
        rejected = tuple(sorted(set(self.rejected_operation_ids)))
        for operation_id in (*approved, *rejected):
            _identity(operation_id, "invalid_operation_id")
        if set(approved).intersection(rejected):
            raise IngestionApprovalError("approval_operation_overlap")
        object.__setattr__(self, "approved_operation_ids", approved)
        object.__setattr__(self, "rejected_operation_ids", rejected)
        if self.approval_scope not in APPROVAL_SCOPES:
            raise IngestionApprovalError("invalid_approval_scope")
        _token(self.actor_kind, "invalid_actor_kind")
        created = _timestamp(self.created_at, "invalid_approval_created_at")
        expires = _timestamp(self.expires_at, "invalid_approval_expires_at")
        if expires <= created:
            raise IngestionApprovalError("invalid_approval_interval")
        _sha(self.approval_digest, "invalid_approval_digest")
        if self.approval_schema_version != APPROVAL_SCHEMA_VERSION:
            raise IngestionApprovalError("approval_schema_version_mismatch")
        expected = normalized_json_digest(self.identity_payload())
        if self.approval_digest != expected:
            raise IngestionApprovalError("approval_digest_mismatch")
        if self.approval_id != "approval_" + expected:
            raise IngestionApprovalError("approval_identity_mismatch")

    def identity_payload(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "manifest_id": self.manifest_id,
            "plan_id": self.plan_id,
            "plan_digest": self.plan_digest,
            "stage_plan_id": self.stage_plan_id,
            "stage_plan_digest": self.stage_plan_digest,
            "target_snapshot_fingerprint": self.target_snapshot_fingerprint,
            "conflict_decisions": [
                item.to_dict() for item in self.conflict_decisions
            ],
            "approved_operation_ids": list(self.approved_operation_ids),
            "rejected_operation_ids": list(self.rejected_operation_ids),
            "approval_scope": self.approval_scope,
            "actor_kind": self.actor_kind,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "approval_schema_version": self.approval_schema_version,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            **self.identity_payload(),
            "approval_digest": self.approval_digest,
        }


def make_approval_artifact(
    *,
    import_plan: Any,
    stage_plan: AssetStagePlan,
    target_snapshot_fingerprint: str,
    candidate_operation_ids: Sequence[str],
    approved_operation_ids: Sequence[str],
    rejected_operation_ids: Sequence[str],
    conflict_decisions: Sequence[ConflictDecision],
    approval_scope: str,
    actor_kind: str,
    created_at: str,
    expires_at: str,
) -> ApprovalArtifact:
    if not _approval_plan_compatible(import_plan):
        raise IngestionApprovalError("invalid_import_plan")
    if not isinstance(stage_plan, AssetStagePlan):
        raise IngestionApprovalError("invalid_asset_stage_plan")
    if (
        stage_plan.batch_id != import_plan.batch_id
        or stage_plan.manifest_id != import_plan.manifest_id
        or stage_plan.import_plan_id != import_plan.plan_id
    ):
        raise IngestionApprovalError("plan_stage_binding_mismatch")
    candidate = set(candidate_operation_ids)
    approved = set(approved_operation_ids)
    rejected = set(rejected_operation_ids)
    if approved.intersection(rejected):
        raise IngestionApprovalError("approval_operation_overlap")
    if not approved.union(rejected).issubset(candidate):
        raise IngestionApprovalError("approval_contains_unplanned_operation")
    if approval_scope == "exact_plan" and approved.union(rejected) != candidate:
        raise IngestionApprovalError("approval_operation_partition_incomplete")
    conflict_by_id = {
        conflict.conflict_id: conflict for conflict in import_plan.conflicts
    }
    decisions_by_id: dict[str, ConflictDecision] = {}
    for decision in conflict_decisions:
        if not isinstance(decision, ConflictDecision):
            raise IngestionApprovalError("invalid_conflict_decision")
        if decision.conflict_id in decisions_by_id:
            raise IngestionApprovalError("duplicate_conflict_decision")
        conflict = conflict_by_id.get(decision.conflict_id)
        if conflict is None:
            raise IngestionApprovalError("conflict_not_in_plan")
        if (
            decision.conflict_type != conflict.conflict_type
            or decision.selected_action not in conflict.allowed_decisions
        ):
            raise IngestionApprovalError("conflict_decision_mismatch")
        expected_evidence_digest = normalized_json_digest({
            "conflict_id": conflict.conflict_id,
            "conflict_type": conflict.conflict_type,
            "risk_level": conflict.risk_level,
            "evidence_hashes": list(conflict.evidence_hashes),
            "selected_action": decision.selected_action,
        })
        if decision.evidence_digest != expected_evidence_digest:
            raise IngestionApprovalError(
                "conflict_decision_evidence_mismatch"
            )
        if not set(decision.allowed_operation_ids).issubset(candidate):
            raise IngestionApprovalError("conflict_contains_unplanned_operation")
        if not set(decision.rejected_operation_ids).issubset(candidate):
            raise IngestionApprovalError("conflict_contains_unplanned_operation")
        if not set(decision.allowed_operation_ids).issubset(approved):
            raise IngestionApprovalError("conflict_approval_mismatch")
        if not set(decision.rejected_operation_ids).issubset(rejected):
            raise IngestionApprovalError("conflict_rejection_mismatch")
        if (
            conflict.risk_level in HIGH_RISK_LEVELS
            and decision.actor_kind in {"policy_engine", "automatic"}
        ):
            raise IngestionApprovalError("high_risk_conflict_requires_explicit_actor")
        decisions_by_id[decision.conflict_id] = decision
    if approval_scope == "exact_plan" and set(decisions_by_id) != set(conflict_by_id):
        raise IngestionApprovalError("required_conflict_unresolved")
    _sha(target_snapshot_fingerprint, "invalid_target_snapshot_fingerprint")
    payload = {
        "batch_id": import_plan.batch_id,
        "manifest_id": import_plan.manifest_id,
        "plan_id": import_plan.plan_id,
        "plan_digest": import_plan_digest(import_plan),
        "stage_plan_id": stage_plan.stage_plan_id,
        "stage_plan_digest": stage_plan.stage_plan_digest,
        "target_snapshot_fingerprint": target_snapshot_fingerprint,
        "conflict_decisions": [
            item.to_dict()
            for item in sorted(conflict_decisions, key=lambda item: item.conflict_id)
        ],
        "approved_operation_ids": sorted(approved),
        "rejected_operation_ids": sorted(rejected),
        "approval_scope": approval_scope,
        "actor_kind": actor_kind,
        "created_at": created_at,
        "expires_at": expires_at,
        "approval_schema_version": APPROVAL_SCHEMA_VERSION,
    }
    digest = normalized_json_digest(payload)
    return ApprovalArtifact(
        approval_id="approval_" + digest,
        approval_digest=digest,
        **payload,
    )


def validate_approval_artifact(
    approval: ApprovalArtifact,
    *,
    import_plan: Any,
    stage_plan: AssetStagePlan,
    target_snapshot_fingerprint: str,
    candidate_operation_ids: Sequence[str],
    now: str,
    required_scope: str = "exact_plan",
) -> None:
    if not isinstance(approval, ApprovalArtifact):
        raise IngestionApprovalError("invalid_approval")
    serialized = approval.to_dict()
    serialized["conflict_decisions"] = tuple(
        ConflictDecision(**item)
        for item in serialized["conflict_decisions"]
    )
    ApprovalArtifact(**serialized)
    if approval.approval_scope != required_scope:
        raise IngestionApprovalError("approval_scope_mismatch")
    if (
        approval.batch_id != import_plan.batch_id
        or approval.manifest_id != import_plan.manifest_id
        or approval.plan_id != import_plan.plan_id
        or approval.plan_digest != import_plan_digest(import_plan)
    ):
        raise IngestionApprovalError("approval_plan_stale")
    if (
        approval.stage_plan_id != stage_plan.stage_plan_id
        or approval.stage_plan_digest != stage_plan.stage_plan_digest
    ):
        raise IngestionApprovalError("approval_stage_stale")
    if approval.target_snapshot_fingerprint != target_snapshot_fingerprint:
        raise IngestionApprovalError("approval_target_stale")
    if set(approval.approved_operation_ids).union(
        approval.rejected_operation_ids
    ) != set(candidate_operation_ids):
        raise IngestionApprovalError("approval_operation_set_stale")
    current = _timestamp(now, "invalid_preflight_time")
    expires = _timestamp(approval.expires_at, "invalid_approval_expires_at")
    if current >= expires:
        raise IngestionApprovalError("approval_expired")
    expected = make_approval_artifact(
        import_plan=import_plan,
        stage_plan=stage_plan,
        target_snapshot_fingerprint=target_snapshot_fingerprint,
        candidate_operation_ids=candidate_operation_ids,
        approved_operation_ids=approval.approved_operation_ids,
        rejected_operation_ids=approval.rejected_operation_ids,
        conflict_decisions=approval.conflict_decisions,
        approval_scope=approval.approval_scope,
        actor_kind=approval.actor_kind,
        created_at=approval.created_at,
        expires_at=approval.expires_at,
    )
    if (
        expected.approval_id != approval.approval_id
        or expected.approval_digest != approval.approval_digest
    ):
        raise IngestionApprovalError("approval_revalidation_mismatch")
