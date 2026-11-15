"""Read-only relation candidate discovery and human-review envelopes for YunOS v6.1."""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from memory.graph_identity import GraphEdge, GraphNode, canonical_hash
from memory.memory_graph import MemoryGraphBuild


CANDIDATE_SCHEMA_VERSION = "v6.1"
REVIEW_SCHEMA_VERSION = "v6.1"
DECISION_SCHEMA_VERSION = "v6.1"
SUPERSESSION_SCHEMA_VERSION = "v6.1"
REPLACEMENT_POLICY_IDENTITY = "same_relation_direction_endpoints_v1"
REVIEW_POLICY_IDENTITY = "yunos_relation_review_policy_v2_same_relation_direction_endpoints"
AUTHORITY_PROTOCOL_IDENTITY = "yunos_relation_review_authority_v1"
AUTHORITY_SCHEMA_VERSION = "v6.1-authority-2"
ALLOWED_DECISIONS = ("accept", "reject", "defer", "replace")
REVIEWABLE_STATUSES = {"candidate", "manual_review"}
CANDIDATE_STATUSES = {
    "candidate", "deterministic", "unavailable", "manual_review", "superseded",
    "stale", "rejected", "accepted_pending_persistence", "deferred",
}
SUPPORTED_RELATION_TYPES = {"belongs_to_conversation", "belongs_to_memory_space", "newer_version_of", "semantic_related"}
SUPPORTED_DIRECTIONS = {"directed", "undirected"}
SUPPORTED_EVIDENCE_KINDS = {"semantic_embedding", "graph_identity"}
SUPPORTED_PLAN_STATUSES = {"ready"}
SUPPORTED_REASON_CODES = {"reviewed", "reviewed_evidence", "evidence_reviewed", "insufficient_evidence", "duplicate_candidate", "defer_for_more_evidence", "replacement_preferred", "seal_review"}
SUPPORTED_RATIONALE_CODES = {
    "threshold_qualified_semantic_evidence", "deterministic_graph_relation",
    "summary_identity_unavailable", "summary_version_gap", "embedding_missing",
    "embedding_content_mismatch", "embedding_metadata_mismatch",
    "embedding_metadata_invalid", "embedding_metadata_missing",
    "embedding_identity_incompatible", "embedding_vector_invalid",
    "embedding_unavailable", "semantic_score_below_threshold",
}
_DISCOVERY_SEAL = object()
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_PREFIXED64 = re.compile(r"^[a-z][a-z0-9_]*_[0-9a-f]{64}$")
class RelationReviewError(ValueError):
    """Raised when an untrusted, incomplete, conflicting, or stale review is supplied."""


@dataclass(frozen=True)
class RelationCandidate:
    candidate_schema_version: str
    candidate_id: str
    relation_type: str
    source_node_id: str
    target_node_id: str
    direction: str
    source_node_payload_hash: str
    target_node_payload_hash: str
    generator_identity: str
    generator_config_hash: str
    evidence_digest: str
    evidence_kind: str
    score: float | None
    threshold: float | None
    rationale_codes: tuple[str, ...]
    candidate_status: str
    graph_build_identity: str
    created_at: str

    def public_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["rationale_codes"] = list(self.rationale_codes)
        return value

    def immutable_payload(self) -> dict[str, Any]:
        value = self.public_dict()
        value.pop("candidate_id")
        value.pop("created_at")
        return value

    @property
    def payload_hash(self) -> str:
        return canonical_hash("relation_candidate_payload", self.immutable_payload())


@dataclass(frozen=True)
class CandidateUnavailable:
    relation_type: str
    candidate_status: str
    evidence_digest: str
    rationale_codes: tuple[str, ...]
    source_node_id: str | None
    target_node_id: str | None
    score: float | None
    threshold: float | None
    graph_build_identity: str

    def public_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["rationale_codes"] = list(self.rationale_codes)
        return value


@dataclass(frozen=True)
class CandidateDiscovery:
    graph_build_identity: str
    nodes: tuple[GraphNode, ...]
    candidates: tuple[RelationCandidate, ...]
    unavailable: tuple[CandidateUnavailable, ...]
    _provenance_seal: object = field(repr=False, compare=False)

    def assert_trusted(self) -> None:
        if self._provenance_seal is not _DISCOVERY_SEAL:
            raise RelationReviewError("candidate discovery provenance is not trusted")

    def candidate(self, candidate_id: str) -> RelationCandidate:
        found = [item for item in self.candidates if item.candidate_id == candidate_id]
        if len(found) != 1:
            raise RelationReviewError("candidate is unavailable or not uniquely discovered")
        return found[0]


@dataclass(frozen=True)
class ReviewPlan:
    review_schema_version: str
    review_plan_id: str
    authority_schema_version: str
    authority_store_id: str
    authority_generation: str
    authority_metadata_digest: str
    authority_protocol_identity: str
    review_session_id: str
    review_chain_id: str
    predecessor_review_chain_id: str | None
    candidate_id: str
    candidate_payload_hash: str
    source_node_id: str
    source_node_payload_hash: str
    target_node_id: str
    target_node_payload_hash: str
    relation_type: str
    direction: str
    evidence_digest: str
    generator_identity: str
    generator_config_hash: str
    graph_build_identity: str
    score: float | None
    threshold: float | None
    review_policy_identity: str
    allowed_decisions: tuple[str, ...]
    plan_status: str
    generated_at: str

    def public_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["allowed_decisions"] = list(self.allowed_decisions)
        return value

    def immutable_payload(self) -> dict[str, Any]:
        value = self.public_dict()
        value.pop("review_plan_id")
        value.pop("generated_at")
        return value

    @property
    def payload_hash(self) -> str:
        return canonical_hash("relation_review_plan_payload", self.immutable_payload())


@dataclass(frozen=True)
class DecisionEnvelope:
    decision_schema_version: str
    decision_id: str
    review_plan_id: str
    candidate_id: str
    candidate_payload_hash: str
    authority_schema_version: str
    authority_store_id: str
    authority_generation: str
    authority_metadata_digest: str
    authority_protocol_identity: str
    review_session_id: str
    review_chain_id: str
    relation_type: str
    source_node_id: str
    target_node_id: str
    generator_identity: str
    evidence_digest: str
    decision: str
    reason_code: str
    reviewer_identity: str
    confirmation_fields: dict[str, Any]
    replacement_candidate_id: str | None
    replacement_review_plan_id: str | None
    replacement_candidate_payload_hash: str | None
    replacement_plan_payload_hash: str | None
    replacement_policy_identity: str | None
    decided_at: str
    status: str
    next_action: str
    persisted: bool = False
    readonly: bool = True
    coordination_recorded: bool = True
    review_state: str = "recorded"

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)

    def immutable_payload(self) -> dict[str, Any]:
        value = self.public_dict()
        for key in (
            "decision_id", "decided_at", "status", "next_action", "persisted",
            "readonly", "coordination_recorded", "review_state",
        ):
            value.pop(key)
        return value

    @property
    def payload_hash(self) -> str:
        return canonical_hash("relation_review_decision_payload", self.immutable_payload())


@dataclass(frozen=True)
class SupersessionRecord:
    supersession_schema_version: str
    supersession_id: str
    decision_id: str
    authority_schema_version: str
    authority_store_id: str
    authority_generation: str
    authority_metadata_digest: str
    authority_protocol_identity: str
    review_session_id: str
    original_review_chain_id: str
    replacement_review_chain_id: str
    original_candidate_id: str
    original_candidate_payload_hash: str
    original_review_plan_id: str
    original_plan_payload_hash: str
    replacement_candidate_id: str
    replacement_candidate_payload_hash: str
    replacement_review_plan_id: str
    replacement_plan_payload_hash: str
    replacement_policy_identity: str
    recorded_at: str

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)

    def immutable_payload(self) -> dict[str, Any]:
        value = self.public_dict()
        value.pop("supersession_id")
        value.pop("recorded_at")
        return value


@dataclass(frozen=True)
class ReviewPreparation:
    status: str
    plan: ReviewPlan
    next_action: str
    coordination_recorded: bool = True
    persisted: bool = False
    readonly: bool = True

    def public_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "review_plan": self.plan.public_dict(),
            "review_plan_id": self.plan.review_plan_id,
            "candidate_id": self.plan.candidate_id,
            "review_session_id": self.plan.review_session_id,
            "review_chain_id": self.plan.review_chain_id,
            "authority_id_digest": hashlib.sha256(self.plan.authority_store_id.encode("utf-8")).hexdigest(),
            "next_action": self.next_action,
            "coordination_recorded": self.coordination_recorded,
            "persisted": self.persisted,
            "readonly": self.readonly,
        }


def discover_relation_candidates(build: MemoryGraphBuild, *, created_at: str | None = None) -> CandidateDiscovery:
    """Convert a trusted v6.0 graph build into immutable review candidates."""
    timestamp = created_at or _utc_now()
    graph_identity = graph_build_identity(build)
    nodes = {node.node_id: node for node in build.nodes}
    candidates: dict[str, RelationCandidate] = {}
    unavailable: list[CandidateUnavailable] = []
    for edge in build.edges:
        if edge.source_node_id not in nodes or edge.target_node_id not in nodes:
            unavailable.append(_edge_unavailable(edge, graph_identity, "endpoint_identity_unavailable"))
            continue
        candidate = _candidate_from_edge(edge, nodes, graph_identity, timestamp)
        candidates.setdefault(candidate.candidate_id, candidate)
    for item in build.unavailable:
        unavailable.append(CandidateUnavailable(
            relation_type=item.relation_type,
            candidate_status="unavailable",
            evidence_digest=_digest_identity(item.evidence_identity),
                rationale_codes=(_rationale_code(item.reason),),
            source_node_id=item.source_node_id,
            target_node_id=item.target_node_id,
            score=item.score,
            threshold=item.threshold,
            graph_build_identity=graph_identity,
        ))
    return CandidateDiscovery(
        graph_build_identity=graph_identity,
        nodes=tuple(sorted(build.nodes, key=lambda node: node.node_id)),
        candidates=tuple(sorted(candidates.values(), key=lambda item: item.candidate_id)),
        unavailable=tuple(sorted(unavailable, key=lambda item: (item.relation_type, item.evidence_digest))),
        _provenance_seal=_DISCOVERY_SEAL,
    )


def graph_build_identity(build: MemoryGraphBuild) -> str:
    payload = {
        "schema_version": build.schema_version,
        "generator_config_hash": build.generator_config_hash,
        "node_ids": sorted(node.node_id for node in build.nodes),
        "edge_ids": sorted(edge.edge_id for edge in build.edges),
        "unavailable_evidence": sorted(item.evidence_identity for item in build.unavailable),
    }
    return f"build_{canonical_hash('relation_review_graph_build', payload)}"


def prepare_review_plan(
    discovery: CandidateDiscovery,
    candidate_id: str,
    *,
    authority: Any,
    generated_at: str | None = None,
) -> ReviewPreparation:
    discovery.assert_trusted()
    _require_authority(authority)
    candidate = discovery.candidate(candidate_id)
    if candidate.candidate_status not in REVIEWABLE_STATUSES:
        raise RelationReviewError("candidate status is not eligible for human review")
    result = authority._prepare_plan(
        candidate,
        lambda context: _build_review_plan(candidate, context, generated_at),
    )
    return ReviewPreparation(
        status=result["status"],
        plan=result["plan"],
        next_action=result["next_action"],
    )


def _build_review_plan(
    candidate: RelationCandidate,
    authority_context: dict[str, Any],
    generated_at: str | None,
) -> ReviewPlan:
    payload = {
        "review_schema_version": REVIEW_SCHEMA_VERSION,
        "authority_schema_version": authority_context["authority_schema_version"],
        "authority_store_id": authority_context["authority_store_id"],
        "authority_generation": authority_context["authority_generation"],
        "authority_metadata_digest": authority_context["authority_metadata_digest"],
        "authority_protocol_identity": authority_context["authority_protocol_identity"],
        "review_session_id": authority_context["review_session_id"],
        "review_chain_id": authority_context["review_chain_id"],
        "predecessor_review_chain_id": authority_context.get("predecessor_review_chain_id"),
        "candidate_id": candidate.candidate_id,
        "candidate_payload_hash": candidate.payload_hash,
        "source_node_id": candidate.source_node_id,
        "source_node_payload_hash": candidate.source_node_payload_hash,
        "target_node_id": candidate.target_node_id,
        "target_node_payload_hash": candidate.target_node_payload_hash,
        "relation_type": candidate.relation_type,
        "direction": candidate.direction,
        "evidence_digest": candidate.evidence_digest,
        "generator_identity": candidate.generator_identity,
        "generator_config_hash": candidate.generator_config_hash,
        "graph_build_identity": candidate.graph_build_identity,
        "score": candidate.score,
        "threshold": candidate.threshold,
        "review_policy_identity": REVIEW_POLICY_IDENTITY,
        "allowed_decisions": list(ALLOWED_DECISIONS),
        "plan_status": "ready",
    }
    plan_id = f"plan_{canonical_hash('relation_review_plan', payload)}"
    payload["allowed_decisions"] = tuple(payload["allowed_decisions"])
    return ReviewPlan(review_plan_id=plan_id, generated_at=generated_at or _utc_now(), **payload)


def verify_review_plan(
    discovery: CandidateDiscovery,
    plan: ReviewPlan,
    *,
    authority: Any,
) -> dict[str, Any]:
    discovery.assert_trusted()
    _require_authority(authority)
    binding = _verify_plan_binding(discovery, plan, authority)
    if binding["status"] != "valid":
        return binding
    return authority._inspect_plan(plan)


def _verify_plan_binding(
    discovery: CandidateDiscovery,
    plan: ReviewPlan,
    authority: Any,
) -> dict[str, Any]:
    try:
        candidate = discovery.candidate(plan.candidate_id)
    except RelationReviewError:
        return _verification("stale", "candidate_missing_or_superseded", "prepare_again")
    try:
        if candidate.candidate_status not in REVIEWABLE_STATUSES:
            raise RelationReviewError("candidate is not reviewable")
        authority.assert_plan_identity(plan)
        expected = _build_review_plan(candidate, {
            "authority_schema_version": plan.authority_schema_version,
            "authority_store_id": plan.authority_store_id,
            "authority_generation": plan.authority_generation,
            "authority_metadata_digest": plan.authority_metadata_digest,
            "authority_protocol_identity": plan.authority_protocol_identity,
            "review_session_id": plan.review_session_id,
            "review_chain_id": plan.review_chain_id,
            "predecessor_review_chain_id": plan.predecessor_review_chain_id,
        }, plan.generated_at)
    except RelationReviewError:
        return _verification("unavailable", "candidate_not_reviewable", "manual_review")
    if expected.review_plan_id != plan.review_plan_id or expected.immutable_payload() != plan.immutable_payload():
        return _verification("stale", "bound_review_state_changed", "prepare_again")
    return _verification("valid", "review_plan_matches_current_candidate", "decide")


def required_confirmation_fields(
    plan: ReviewPlan,
    decision: str,
    *,
    replacement_candidate: RelationCandidate | None = None,
    replacement_plan: ReviewPlan | None = None,
) -> dict[str, Any]:
    fields = {
        "review_plan_id": plan.review_plan_id,
        "candidate_id": plan.candidate_id,
        "candidate_payload_hash": plan.candidate_payload_hash,
        "authority_schema_version": plan.authority_schema_version,
        "authority_store_id": plan.authority_store_id,
        "authority_generation": plan.authority_generation,
        "authority_metadata_digest": plan.authority_metadata_digest,
        "authority_protocol_identity": plan.authority_protocol_identity,
        "review_session_id": plan.review_session_id,
        "review_chain_id": plan.review_chain_id,
        "source_node_id": plan.source_node_id,
        "source_node_payload_hash": plan.source_node_payload_hash,
        "target_node_id": plan.target_node_id,
        "target_node_payload_hash": plan.target_node_payload_hash,
        "relation_type": plan.relation_type,
        "direction": plan.direction,
        "evidence_digest": plan.evidence_digest,
        "generator_identity": plan.generator_identity,
        "generator_config_hash": plan.generator_config_hash,
        "graph_build_identity": plan.graph_build_identity,
        "review_policy_identity": plan.review_policy_identity,
        "score": plan.score,
        "threshold": plan.threshold,
        "decision": decision,
    }
    if decision == "replace":
        if replacement_candidate is None or replacement_plan is None:
            raise RelationReviewError("replace confirmation requires replacement candidate and plan")
        fields.update({
            "replacement_candidate_id": replacement_candidate.candidate_id,
            "replacement_candidate_payload_hash": replacement_candidate.payload_hash,
            "replacement_review_plan_id": replacement_plan.review_plan_id,
            "replacement_plan_payload_hash": replacement_plan.payload_hash,
            "replacement_relation_type": replacement_candidate.relation_type,
            "replacement_direction": replacement_candidate.direction,
            "replacement_source_node_id": replacement_candidate.source_node_id,
            "replacement_source_node_payload_hash": replacement_candidate.source_node_payload_hash,
            "replacement_target_node_id": replacement_candidate.target_node_id,
            "replacement_target_node_payload_hash": replacement_candidate.target_node_payload_hash,
            "replacement_evidence_digest": replacement_candidate.evidence_digest,
            "replacement_graph_build_identity": replacement_candidate.graph_build_identity,
            "replacement_policy_identity": REPLACEMENT_POLICY_IDENTITY,
        })
    elif replacement_candidate is not None or replacement_plan is not None:
        raise RelationReviewError("replacement confirmation is valid only for replace")
    return fields


def decide_relation_candidate(
    discovery: CandidateDiscovery,
    plan: ReviewPlan,
    *,
    decision: str,
    reason_code: str,
    reviewer_identity: str,
    confirmation_fields: dict[str, Any],
    authority: Any,
    replacement_candidate_id: str | None = None,
    replacement_plan: ReviewPlan | None = None,
    decided_at: str | None = None,
) -> dict[str, Any]:
    discovery.assert_trusted()
    _require_authority(authority)
    if decision not in ALLOWED_DECISIONS:
        raise RelationReviewError("decision is unsupported")
    verification = _verify_plan_binding(discovery, plan, authority)
    if verification["status"] != "valid":
        raise RelationReviewError("review plan is stale or unavailable")
    candidate = discovery.candidate(plan.candidate_id)
    if candidate.candidate_status not in REVIEWABLE_STATUSES:
        raise RelationReviewError("candidate cannot enter the decision workflow")
    replacement: RelationCandidate | None = None
    if decision == "replace":
        if not replacement_candidate_id or replacement_candidate_id == plan.candidate_id or replacement_plan is None:
            raise RelationReviewError("replace requires a distinct discovered candidate and new review plan")
        replacement = discovery.candidate(replacement_candidate_id)
        replacement_verification = _verify_plan_binding(discovery, replacement_plan, authority)
        if replacement_plan.candidate_id != replacement.candidate_id or replacement_verification["status"] != "valid":
            raise RelationReviewError("replacement candidate requires its own current review plan")
        _validate_replacement_policy(candidate, replacement)
        status, next_action = "superseded", "review_replacement"
    else:
        if replacement_candidate_id is not None or replacement_plan is not None:
            raise RelationReviewError("replacement fields are only valid for replace")
        status, next_action = {
            "accept": ("accepted_pending_persistence", "persist_in_v6_2"),
            "reject": ("rejected", "none"),
            "defer": ("deferred", "prepare_again_before_next_review"),
        }[decision]
    expected = required_confirmation_fields(
        plan,
        decision,
        replacement_candidate=replacement,
        replacement_plan=replacement_plan,
    )
    if confirmation_fields != expected:
        raise RelationReviewError("complete exact confirmation fields are required")
    reason = _reason_code(reason_code)
    reviewer_digest = _reviewer_digest(reviewer_identity)
    identity = {
        "decision_schema_version": DECISION_SCHEMA_VERSION,
        "review_plan_id": plan.review_plan_id,
        "candidate_id": plan.candidate_id,
        "candidate_payload_hash": plan.candidate_payload_hash,
        "authority_schema_version": plan.authority_schema_version,
        "authority_store_id": plan.authority_store_id,
        "authority_generation": plan.authority_generation,
        "authority_metadata_digest": plan.authority_metadata_digest,
        "authority_protocol_identity": plan.authority_protocol_identity,
        "review_session_id": plan.review_session_id,
        "review_chain_id": plan.review_chain_id,
        "relation_type": plan.relation_type,
        "source_node_id": plan.source_node_id,
        "target_node_id": plan.target_node_id,
        "generator_identity": plan.generator_identity,
        "evidence_digest": plan.evidence_digest,
        "decision": decision,
        "reason_code": reason,
        "reviewer_identity": reviewer_digest,
        "confirmation_fields": expected,
        "replacement_candidate_id": replacement_candidate_id,
        "replacement_review_plan_id": replacement_plan.review_plan_id if replacement_plan else None,
        "replacement_candidate_payload_hash": replacement.payload_hash if replacement else None,
        "replacement_plan_payload_hash": replacement_plan.payload_hash if replacement_plan else None,
        "replacement_policy_identity": REPLACEMENT_POLICY_IDENTITY if replacement else None,
    }
    envelope = DecisionEnvelope(
        decision_schema_version=DECISION_SCHEMA_VERSION,
        decision_id=f"decision_{canonical_hash('relation_review_decision', identity)}",
        review_plan_id=plan.review_plan_id,
        candidate_id=plan.candidate_id,
        candidate_payload_hash=plan.candidate_payload_hash,
        authority_schema_version=plan.authority_schema_version,
        authority_store_id=plan.authority_store_id,
        authority_generation=plan.authority_generation,
        authority_metadata_digest=plan.authority_metadata_digest,
        authority_protocol_identity=plan.authority_protocol_identity,
        review_session_id=plan.review_session_id,
        review_chain_id=plan.review_chain_id,
        relation_type=plan.relation_type,
        source_node_id=plan.source_node_id,
        target_node_id=plan.target_node_id,
        generator_identity=plan.generator_identity,
        evidence_digest=plan.evidence_digest,
        decision=decision,
        reason_code=reason,
        reviewer_identity=reviewer_digest,
        confirmation_fields=expected,
        replacement_candidate_id=replacement_candidate_id,
        replacement_review_plan_id=replacement_plan.review_plan_id if replacement_plan else None,
        replacement_candidate_payload_hash=replacement.payload_hash if replacement else None,
        replacement_plan_payload_hash=replacement_plan.payload_hash if replacement_plan else None,
        replacement_policy_identity=REPLACEMENT_POLICY_IDENTITY if replacement else None,
        decided_at=decided_at or _utc_now(),
        status=status,
        next_action=next_action,
    )
    supersession = _supersession_record(envelope, candidate, plan, replacement, replacement_plan) if replacement and replacement_plan else None
    result = authority._commit_decision(
        candidate,
        plan,
        envelope,
        replacement_candidate=replacement,
        replacement_plan=replacement_plan,
        supersession=supersession,
    )
    if result["status"] in {"decision_conflict", "superseded", "stale", "manual_review"}:
        return {
            "status": result["status"],
            "reason_code": result.get("reason_code", result["status"]),
            "next_action": result.get("next_action", "manual_review"),
            "persisted": False,
            "readonly": True,
            "coordination_recorded": bool(result.get("coordination_recorded", False)),
            "review_state": result.get("review_state", "conflict"),
        }
    stored: DecisionEnvelope = result["envelope"]
    payload = stored.public_dict()
    payload["idempotent"] = result["status"] == "idempotent"
    return payload


def replacement_preview(
    discovery: CandidateDiscovery,
    original_candidate_id: str,
    replacement_candidate_id: str,
    *,
    original_plan: ReviewPlan,
    replacement_plan: ReviewPlan,
    authority: Any,
) -> dict[str, Any]:
    discovery.assert_trusted()
    _require_authority(authority)
    original = discovery.candidate(original_candidate_id)
    replacement = discovery.candidate(replacement_candidate_id)
    if original.candidate_id == replacement.candidate_id:
        raise RelationReviewError("replacement candidate must be distinct")
    if original_plan.candidate_id != original.candidate_id:
        raise RelationReviewError("original review plan does not bind original candidate")
    current = verify_review_plan(
        discovery,
        original_plan,
        authority=authority,
    )
    if current["status"] != "valid":
        raise RelationReviewError("original review plan is not current")
    _validate_replacement_policy(original, replacement)
    if replacement_plan.candidate_id != replacement.candidate_id:
        raise RelationReviewError("replacement plan does not bind replacement candidate")
    replacement_state = verify_review_plan(discovery, replacement_plan, authority=authority)
    if replacement_state["status"] != "valid":
        raise RelationReviewError("replacement plan is not active")
    return {
        "status": "replacement_preview",
        "readonly": True,
        "persisted": False,
        "original_candidate_id": original.candidate_id,
        "current_status": original.candidate_status,
        "proposed_effect": "would_supersede",
        "replacement_candidate": replacement.public_dict(),
        "replacement_review_plan": replacement_plan.public_dict(),
        "replacement_plan_payload_hash": replacement_plan.payload_hash,
        "replacement_policy_identity": REPLACEMENT_POLICY_IDENTITY,
        "coordination_recorded": False,
        "review_state": "preview_only",
        "next_action": "review_replacement",
    }


def _validate_replacement_policy(original: RelationCandidate, replacement: RelationCandidate) -> None:
    if replacement.candidate_status not in REVIEWABLE_STATUSES:
        raise RelationReviewError("replacement candidate is not reviewable")
    original_family = (
        original.relation_type, original.direction,
        original.source_node_id, original.target_node_id,
    )
    replacement_family = (
        replacement.relation_type, replacement.direction,
        replacement.source_node_id, replacement.target_node_id,
    )
    if original_family != replacement_family:
        raise RelationReviewError("replacement must preserve relation type, direction, and endpoints")


def _supersession_record(
    envelope: DecisionEnvelope,
    original: RelationCandidate,
    original_plan: ReviewPlan,
    replacement: RelationCandidate,
    replacement_plan: ReviewPlan,
) -> SupersessionRecord:
    payload = {
        "supersession_schema_version": SUPERSESSION_SCHEMA_VERSION,
        "decision_id": envelope.decision_id,
        "authority_schema_version": original_plan.authority_schema_version,
        "authority_store_id": original_plan.authority_store_id,
        "authority_generation": original_plan.authority_generation,
        "authority_metadata_digest": original_plan.authority_metadata_digest,
        "authority_protocol_identity": original_plan.authority_protocol_identity,
        "review_session_id": original_plan.review_session_id,
        "original_review_chain_id": original_plan.review_chain_id,
        "replacement_review_chain_id": replacement_plan.review_chain_id,
        "original_candidate_id": original.candidate_id,
        "original_candidate_payload_hash": original.payload_hash,
        "original_review_plan_id": original_plan.review_plan_id,
        "original_plan_payload_hash": original_plan.payload_hash,
        "replacement_candidate_id": replacement.candidate_id,
        "replacement_candidate_payload_hash": replacement.payload_hash,
        "replacement_review_plan_id": replacement_plan.review_plan_id,
        "replacement_plan_payload_hash": replacement_plan.payload_hash,
        "replacement_policy_identity": REPLACEMENT_POLICY_IDENTITY,
    }
    return SupersessionRecord(
        supersession_id=f"supersession_{canonical_hash('relation_review_supersession', payload)}",
        recorded_at=envelope.decided_at,
        **payload,
    )


def _require_authority(authority: Any) -> None:
    from memory.relation_review_state import is_trusted_review_authority_handle
    if not is_trusted_review_authority_handle(authority):
        raise RelationReviewError("trusted review authority handle is required")
    authority.assert_trusted()


def _candidate_from_edge(
    edge: GraphEdge,
    nodes: dict[str, GraphNode],
    graph_identity: str,
    created_at: str,
) -> RelationCandidate:
    source_id, target_id = edge.source_node_id, edge.target_node_id
    if edge.direction == "undirected" and target_id < source_id:
        source_id, target_id = target_id, source_id
    source_hash = _node_payload_hash(nodes[source_id])
    target_hash = _node_payload_hash(nodes[target_id])
    payload = {
        "candidate_schema_version": CANDIDATE_SCHEMA_VERSION,
        "relation_type": edge.relation_type,
        "source_node_id": source_id,
        "target_node_id": target_id,
        "direction": edge.direction,
        "source_node_payload_hash": source_hash,
        "target_node_payload_hash": target_hash,
        "generator_identity": edge.generator_identity,
        "generator_config_hash": edge.generator_config_hash,
        "evidence_digest": _digest_identity(edge.evidence_identity),
        "evidence_kind": "semantic_embedding" if edge.relation_type == "semantic_related" else "graph_identity",
        "score": edge.score,
        "threshold": edge.threshold,
        "rationale_codes": ["threshold_qualified_semantic_evidence"] if edge.relation_type == "semantic_related" else ["deterministic_graph_relation"],
        "candidate_status": edge.status,
        "graph_build_identity": graph_identity,
    }
    candidate_id = f"candidate_{canonical_hash('relation_candidate', payload)}"
    return RelationCandidate(
        candidate_id=candidate_id,
        rationale_codes=tuple(payload.pop("rationale_codes")),
        created_at=created_at,
        **payload,
    )


def _edge_unavailable(edge: GraphEdge, graph_identity: str, reason: str) -> CandidateUnavailable:
    return CandidateUnavailable(
        relation_type=edge.relation_type,
        candidate_status="unavailable",
        evidence_digest=_digest_identity(edge.evidence_identity),
        rationale_codes=(reason,),
        source_node_id=edge.source_node_id,
        target_node_id=edge.target_node_id,
        score=edge.score,
        threshold=edge.threshold,
        graph_build_identity=graph_identity,
    )


def validate_candidate_identity(candidate: RelationCandidate) -> None:
    if candidate.candidate_schema_version != CANDIDATE_SCHEMA_VERSION:
        raise RelationReviewError("candidate schema version is unsupported")
    _prefixed(candidate.candidate_id, "candidate", "candidate id")
    if candidate.relation_type not in SUPPORTED_RELATION_TYPES or candidate.direction not in SUPPORTED_DIRECTIONS:
        raise RelationReviewError("candidate relation or direction is unsupported")
    if candidate.candidate_status not in CANDIDATE_STATUSES:
        raise RelationReviewError("candidate status is unsupported")
    if candidate.evidence_kind not in SUPPORTED_EVIDENCE_KINDS:
        raise RelationReviewError("candidate evidence kind is unsupported")
    if candidate.relation_type == "semantic_related" and (candidate.direction != "undirected" or candidate.evidence_kind != "semantic_embedding"):
        raise RelationReviewError("semantic candidate protocol is invalid")
    if candidate.direction == "undirected" and candidate.target_node_id < candidate.source_node_id:
        raise RelationReviewError("undirected candidate endpoints are not normalized")
    _prefixed(candidate.source_node_id, "node", "source node id"); _prefixed(candidate.target_node_id, "node", "target node id")
    if candidate.source_node_id == candidate.target_node_id:
        raise RelationReviewError("candidate endpoints must be distinct")
    _hex(candidate.source_node_payload_hash, "source node payload hash"); _hex(candidate.target_node_payload_hash, "target node payload hash")
    _prefixed(candidate.generator_config_hash, "config", "generator config hash"); _hex(candidate.evidence_digest, "evidence digest")
    _prefixed(candidate.graph_build_identity, "build", "graph build identity")
    _finite(candidate.score, "score"); _finite(candidate.threshold, "threshold")
    if not candidate.generator_identity or not candidate.rationale_codes or not all(item in SUPPORTED_RATIONALE_CODES for item in candidate.rationale_codes):
        raise RelationReviewError("candidate generator or rationale is invalid")
    expected = f"candidate_{canonical_hash('relation_candidate', candidate.immutable_payload())}"
    if candidate.candidate_id != expected:
        raise RelationReviewError("candidate identity does not match immutable payload")


def validate_review_plan_identity(plan: ReviewPlan) -> None:
    if plan.review_schema_version != REVIEW_SCHEMA_VERSION:
        raise RelationReviewError("review schema version is unsupported")
    _prefixed(plan.review_plan_id, "plan", "review plan id"); _prefixed(plan.candidate_id, "candidate", "candidate id")
    _prefixed(plan.authority_store_id, "authority", "authority store id"); _prefixed(plan.authority_generation, "generation", "authority generation")
    _hex(plan.authority_metadata_digest, "authority metadata digest")
    if plan.authority_protocol_identity != AUTHORITY_PROTOCOL_IDENTITY or plan.authority_schema_version != AUTHORITY_SCHEMA_VERSION:
        raise RelationReviewError("review authority protocol is unsupported")
    _prefixed(plan.review_session_id, "session", "review session id"); _prefixed(plan.review_chain_id, "chain", "review chain id")
    if plan.predecessor_review_chain_id is not None: _prefixed(plan.predecessor_review_chain_id, "chain", "predecessor chain id")
    _hex(plan.candidate_payload_hash, "candidate payload hash"); _prefixed(plan.source_node_id, "node", "source node id"); _prefixed(plan.target_node_id, "node", "target node id")
    if plan.source_node_id == plan.target_node_id or (plan.direction == "undirected" and plan.target_node_id < plan.source_node_id):
        raise RelationReviewError("review plan endpoints are invalid")
    _hex(plan.source_node_payload_hash, "source node payload hash"); _hex(plan.target_node_payload_hash, "target node payload hash")
    if plan.relation_type not in SUPPORTED_RELATION_TYPES or plan.direction not in SUPPORTED_DIRECTIONS:
        raise RelationReviewError("review plan relation or direction is unsupported")
    _hex(plan.evidence_digest, "evidence digest"); _prefixed(plan.generator_config_hash, "config", "generator config hash"); _prefixed(plan.graph_build_identity, "build", "graph build identity")
    if not isinstance(plan.generator_identity, str) or not plan.generator_identity:
        raise RelationReviewError("review plan generator identity is invalid")
    _finite(plan.score, "score"); _finite(plan.threshold, "threshold")
    if plan.review_policy_identity != REVIEW_POLICY_IDENTITY or tuple(plan.allowed_decisions) != ALLOWED_DECISIONS or plan.plan_status not in SUPPORTED_PLAN_STATUSES:
        raise RelationReviewError("review plan policy or decision allowlist is invalid")
    expected = f"plan_{canonical_hash('relation_review_plan', plan.immutable_payload())}"
    if plan.review_plan_id != expected:
        raise RelationReviewError("review plan identity does not match immutable payload")


def validate_decision_identity(envelope: DecisionEnvelope) -> None:
    if envelope.decision_schema_version != DECISION_SCHEMA_VERSION:
        raise RelationReviewError("decision schema version is unsupported")
    _prefixed(envelope.decision_id, "decision", "decision id"); _prefixed(envelope.review_plan_id, "plan", "review plan id"); _prefixed(envelope.candidate_id, "candidate", "candidate id")
    _hex(envelope.candidate_payload_hash, "candidate payload hash"); _prefixed(envelope.authority_store_id, "authority", "authority store id"); _prefixed(envelope.authority_generation, "generation", "authority generation"); _hex(envelope.authority_metadata_digest, "authority metadata digest")
    if envelope.authority_schema_version != AUTHORITY_SCHEMA_VERSION or envelope.authority_protocol_identity != AUTHORITY_PROTOCOL_IDENTITY or envelope.decision not in ALLOWED_DECISIONS or envelope.reason_code not in SUPPORTED_REASON_CODES:
        raise RelationReviewError("decision protocol value is unsupported")
    _prefixed(envelope.review_session_id, "session", "review session id"); _prefixed(envelope.review_chain_id, "chain", "review chain id")
    _prefixed(envelope.source_node_id, "node", "source node id"); _prefixed(envelope.target_node_id, "node", "target node id")
    _hex(envelope.evidence_digest, "evidence digest"); _prefixed(envelope.reviewer_identity, "reviewer", "reviewer identity")
    if not envelope.generator_identity or envelope.relation_type not in SUPPORTED_RELATION_TYPES:
        raise RelationReviewError("decision candidate fields are invalid")
    expected_outcome = {
        "accept": ("accepted_pending_persistence", "persist_in_v6_2"),
        "reject": ("rejected", "none"),
        "defer": ("deferred", "prepare_again_before_next_review"),
        "replace": ("superseded", "review_replacement"),
    }[envelope.decision]
    if (envelope.status, envelope.next_action) != expected_outcome:
        raise RelationReviewError("decision outcome is inconsistent")
    if envelope.persisted is not False or envelope.readonly is not True or envelope.coordination_recorded is not True:
        raise RelationReviewError("decision persistence flags are invalid")
    replacement_values = (envelope.replacement_candidate_id, envelope.replacement_review_plan_id, envelope.replacement_candidate_payload_hash, envelope.replacement_plan_payload_hash, envelope.replacement_policy_identity)
    if envelope.decision == "replace":
        if any(value is None for value in replacement_values) or envelope.replacement_policy_identity != REPLACEMENT_POLICY_IDENTITY:
            raise RelationReviewError("replacement decision binding is incomplete")
        _prefixed(str(envelope.replacement_candidate_id), "candidate", "replacement candidate id"); _prefixed(str(envelope.replacement_review_plan_id), "plan", "replacement plan id"); _hex(str(envelope.replacement_candidate_payload_hash), "replacement candidate payload hash"); _hex(str(envelope.replacement_plan_payload_hash), "replacement plan payload hash")
    elif any(value is not None for value in replacement_values):
        raise RelationReviewError("non-replacement decision has replacement fields")
    expected = f"decision_{canonical_hash('relation_review_decision', envelope.immutable_payload())}"
    if envelope.decision_id != expected:
        raise RelationReviewError("decision identity does not match immutable payload")


def validate_supersession_identity(record: SupersessionRecord) -> None:
    if record.supersession_schema_version != SUPERSESSION_SCHEMA_VERSION:
        raise RelationReviewError("supersession schema version is unsupported")
    _prefixed(record.supersession_id, "supersession", "supersession id"); _prefixed(record.decision_id, "decision", "decision id")
    _prefixed(record.authority_store_id, "authority", "authority store id"); _prefixed(record.authority_generation, "generation", "authority generation"); _hex(record.authority_metadata_digest, "authority metadata digest")
    if record.authority_schema_version != AUTHORITY_SCHEMA_VERSION or record.authority_protocol_identity != AUTHORITY_PROTOCOL_IDENTITY or record.replacement_policy_identity != REPLACEMENT_POLICY_IDENTITY:
        raise RelationReviewError("supersession protocol identity is invalid")
    for value, prefix, label in ((record.review_session_id,"session","review session id"),(record.original_review_chain_id,"chain","original chain id"),(record.replacement_review_chain_id,"chain","replacement chain id"),(record.original_candidate_id,"candidate","original candidate id"),(record.replacement_candidate_id,"candidate","replacement candidate id"),(record.original_review_plan_id,"plan","original plan id"),(record.replacement_review_plan_id,"plan","replacement plan id")):
        _prefixed(value,prefix,label)
    for value, label in ((record.original_candidate_payload_hash,"original candidate payload hash"),(record.original_plan_payload_hash,"original plan payload hash"),(record.replacement_candidate_payload_hash,"replacement candidate payload hash"),(record.replacement_plan_payload_hash,"replacement plan payload hash")):
        _hex(value,label)
    if record.original_review_chain_id == record.replacement_review_chain_id or record.original_candidate_id == record.replacement_candidate_id:
        raise RelationReviewError("supersession cannot reference itself")
    expected = f"supersession_{canonical_hash('relation_review_supersession', record.immutable_payload())}"
    if record.supersession_id != expected:
        raise RelationReviewError("supersession identity does not match immutable payload")


def _node_payload_hash(node: GraphNode) -> str:
    return canonical_hash("graph_node_payload", node.public_dict())


def _digest_identity(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _reviewer_digest(value: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        raise RelationReviewError("reviewer identity is required")
    return f"reviewer_{canonical_hash('reviewer_identity', {'identity': clean})}"


def _reason_code(value: str) -> str:
    clean = re.sub(r"[^a-z0-9_]+", "_", str(value or "").strip().lower()).strip("_")
    if not clean or len(clean) > 80 or clean not in SUPPORTED_REASON_CODES:
        raise RelationReviewError("reason code is invalid")
    return clean


def _rationale_code(value: str) -> str:
    clean = str(value or "").strip()
    if clean not in SUPPORTED_RATIONALE_CODES:
        raise RelationReviewError("candidate rationale code is unsupported")
    return clean


def _verification(status: str, reason_code: str, next_action: str) -> dict[str, Any]:
    return {"status": status, "reason_code": reason_code, "next_action": next_action, "readonly": True, "persisted": False}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _hex(value: Any, label: str) -> None:
    if not isinstance(value, str) or not _HEX64.fullmatch(value):
        raise RelationReviewError(f"{label} is invalid")


def _prefixed(value: Any, prefix: str, label: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(prefix + r"_[0-9a-f]{64}", value):
        raise RelationReviewError(f"{label} is invalid")


def _finite(value: Any, label: str) -> None:
    if value is None: return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise RelationReviewError(f"candidate {label} is invalid")
