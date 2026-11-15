"""YunOS v7.4 transparent, deterministic context review delivery.

The delivery layer consumes one validated v7.3 context assembly.  It may remove
whole, already-assembled items and their deterministic provenance closure for
one request, but it never changes retrieval, ranking, profile approval,
candidates, history, or any persisted state.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from typing import Any, Iterable

from memory.context_assembly import (
    ContextAssemblyService,
    ContextAssemblyValidationError,
    _render_model_context,
    estimate_tokens,
    validate_context_assembly_standalone,
)
from memory.evidence_selection import ESTIMATOR_PROFILE
from memory.graph_enhanced_retrieval import RetrievalQuery


PROTOCOL_VERSION = "v7.4-transparent-context-delivery-1"
DELIVERY_IDENTITY_VERSION = "v7.4-context-delivery-identity-1"
TEMPORARY_EXCLUSION_POLICY = "v7.4-current-delivery-exclusion-1"


class ContextDeliveryValidationError(ValueError):
    """The v7.4 delivery is invalid or cannot be reproduced."""


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_dump(value).encode("utf-8")).hexdigest()


def _identity_payload(delivery: dict[str, Any]) -> dict[str, Any]:
    return {key: deepcopy(value) for key, value in delivery.items() if key != "delivery_identity"}


def _normalize_exclusions(values: Iterable[str]) -> list[str]:
    if isinstance(values, (str, bytes)):
        raise ContextDeliveryValidationError("excluded_item_ids must be an iterable of item identities")
    normalized: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ContextDeliveryValidationError("excluded item identity must be a non-empty string")
        item_id = value.strip()
        if not re.fullmatch(r"(?:upi|mc|cand|msp|tmr)_[0-9a-f]{64}", item_id):
            raise ContextDeliveryValidationError("excluded item identity is not reviewable")
        normalized.add(item_id)
    return sorted(normalized)


def _source_anchor_for_candidate(item: dict[str, Any]) -> dict[str, Any]:
    uri = item["reference_uri"]
    match = re.fullmatch(r"ayunos://conversation/([^?]+)\?message=([^&]+)", uri)
    return {
        "reference_uri": uri,
        "conversation_id": match.group(1) if match else "",
        "message_id": match.group(2) if match else "",
        "role": "",
        "time": item["observed_at_utc"],
    }


def _source_anchor_for_evidence(item: dict[str, Any]) -> dict[str, Any]:
    anchor = item["message_anchor"]
    return {
        "reference_uri": item["reference_uri"],
        "conversation_id": item["conversation_id"],
        "message_id": anchor["message_id"],
        "role": anchor["role"],
        "time": anchor["time"],
    }


def _provenance(
    *,
    source_ids: Iterable[str] = (),
    evidence_ids: Iterable[str] = (),
    event_ids: Iterable[str] = (),
) -> dict[str, list[str]]:
    return {
        "source_ids": sorted(set(source_ids)),
        "evidence_ids": sorted(set(evidence_ids)),
        "event_ids": sorted(set(event_ids)),
    }


def _provenance_atoms(value: dict[str, list[str]]) -> set[tuple[str, str]]:
    return {
        (kind, item_id)
        for kind in ("source_ids", "evidence_ids", "event_ids")
        for item_id in value[kind]
    }


def _merge_provenance(values: Iterable[dict[str, list[str]]]) -> dict[str, list[str]]:
    merged = {"source_ids": set(), "evidence_ids": set(), "event_ids": set()}
    for value in values:
        for kind in merged:
            merged[kind].update(value[kind])
    return {kind: sorted(items) for kind, items in merged.items()}


def _dependency_provenance(
    source_assembly: dict[str, Any],
) -> tuple[
    dict[str, dict[str, list[str]]],
    dict[str, dict[str, list[str]]],
    dict[str, dict[str, list[str]]],
]:
    evidence_by_reference = {
        item["evidence_reference_id"]: item
        for item in source_assembly["historical_evidence"]["items"]
    }
    candidate_provenance: dict[str, dict[str, list[str]]] = {}
    for item in source_assembly["candidate_memory"]["items"]:
        matching_evidence = evidence_by_reference.get(item["source_candidate_id"])
        candidate_provenance[item["candidate_id"]] = _provenance(
            source_ids=(item["source_candidate_id"],),
            evidence_ids=(matching_evidence or {}).get("evidence_ids", ()),
        )

    evidence_provenance = {
        item["evidence_reference_id"]: _provenance(
            source_ids=(item["evidence_reference_id"],),
            evidence_ids=item["evidence_ids"],
        )
        for item in source_assembly["historical_evidence"]["items"]
    }

    relation_provenance: dict[str, dict[str, list[str]]] = {}
    for item in source_assembly["candidate_memory"]["relations"]:
        endpoint_values = [
            candidate_provenance[item["newer_candidate_id"]],
            candidate_provenance[item["older_candidate_id"]],
        ]
        relation_provenance[item["relation_id"]] = _merge_provenance((
            *endpoint_values,
            _provenance(event_ids=item["evidence_event_ids"]),
        ))
    return candidate_provenance, evidence_provenance, relation_provenance


def _connected_provenance_component(
    seed_item_id: str,
    node_provenance: dict[str, dict[str, list[str]]],
) -> tuple[set[str], set[tuple[str, str]]]:
    component = {seed_item_id}
    atoms = set(_provenance_atoms(node_provenance[seed_item_id]))
    changed = True
    while changed:
        changed = False
        for item_id in sorted(node_provenance):
            if item_id in component:
                continue
            item_atoms = _provenance_atoms(node_provenance[item_id])
            if atoms & item_atoms:
                component.add(item_id)
                atoms.update(item_atoms)
                changed = True
    return component, atoms


def _resolve_exclusion_closure(
    source_assembly: dict[str, Any],
    requested_item_ids: list[str],
) -> dict[str, Any]:
    profile_ids = {
        item["profile_item_id"] for item in source_assembly["confirmed_user_profile"]["items"]
    }
    candidate_provenance, evidence_provenance, relation_provenance = _dependency_provenance(
        source_assembly
    )
    node_provenance = {**candidate_provenance, **evidence_provenance}
    excludable_ids = profile_ids | set(node_provenance)
    unknown = sorted(set(requested_item_ids) - excludable_ids)
    if unknown:
        raise ContextDeliveryValidationError(
            "excluded item is not present in the original assembly: " + ",".join(unknown)
        )

    excluded_profiles = profile_ids & set(requested_item_ids)
    excluded_nodes: set[str] = set()
    excluded_atoms: set[tuple[str, str]] = set()
    atom_causes: dict[tuple[str, str], set[str]] = {}
    causes: dict[str, set[str]] = {item_id: {item_id} for item_id in excluded_profiles}
    for seed_item_id in requested_item_ids:
        if seed_item_id not in node_provenance:
            continue
        component, atoms = _connected_provenance_component(seed_item_id, node_provenance)
        excluded_nodes.update(component)
        excluded_atoms.update(atoms)
        for atom in atoms:
            atom_causes.setdefault(atom, set()).add(seed_item_id)
        for item_id in component:
            causes.setdefault(item_id, set()).add(seed_item_id)

    excluded_candidates = set(candidate_provenance) & excluded_nodes
    excluded_evidence = set(evidence_provenance) & excluded_nodes
    excluded_relations: set[str] = set()
    for item in source_assembly["candidate_memory"]["relations"]:
        item_id = item["relation_id"]
        endpoints = {item["newer_candidate_id"], item["older_candidate_id"]}
        relation_atoms = _provenance_atoms(relation_provenance[item_id])
        if not (endpoints & excluded_candidates or relation_atoms & excluded_atoms):
            continue
        excluded_relations.add(item_id)
        causes[item_id] = {
            cause
            for endpoint in endpoints
            for cause in causes.get(endpoint, set())
        } | {
            cause
            for atom in relation_atoms
            for cause in atom_causes.get(atom, set())
        }

    excluded_item_ids = excluded_profiles | excluded_nodes
    excluded_provenance = _merge_provenance(
        node_provenance[item_id] for item_id in sorted(excluded_nodes)
    )
    return {
        "requested_item_ids": list(requested_item_ids),
        "excluded_item_ids": sorted(excluded_item_ids),
        "excluded_candidate_ids": sorted(excluded_candidates),
        "excluded_evidence_reference_ids": sorted(excluded_evidence),
        "excluded_relation_ids": sorted(excluded_relations),
        "excluded_provenance": excluded_provenance,
        "causes_by_item_id": {
            item_id: sorted(causes[item_id]) for item_id in sorted(causes)
        },
    }


def _model_payload_provenance(
    candidate_items: Iterable[dict[str, Any]],
    candidate_relations: Iterable[dict[str, Any]],
    evidence_items: Iterable[dict[str, Any]],
) -> dict[str, list[str]]:
    values = [item["provenance"] for item in candidate_items]
    values.extend(item["provenance"] for item in candidate_relations)
    for item in evidence_items:
        values.append(item["provenance"])
        if item["expanded_excerpt_provenance"] is not None:
            values.append(item["expanded_excerpt_provenance"])
    return _merge_provenance(values)


def _review_item(
    *,
    layer: str,
    item_type: str,
    item_id: str,
    trust_status: str,
    included: bool,
    excludable: bool,
    content: str,
    source_anchor: dict[str, Any] | None,
    provenance: dict[str, list[str]] | None = None,
    exclusion_state: str = "none",
    exclusion_causes: Iterable[str] = (),
    omitted_reasons: Iterable[str] = (),
) -> dict[str, Any]:
    clean_content = str(content)
    return {
        "layer": layer,
        "item_type": item_type,
        "item_id": item_id,
        "trust_status": trust_status,
        "included": included,
        "excludable": excludable,
        "content": clean_content,
        "characters": len(clean_content),
        "estimated_tokens": estimate_tokens(clean_content),
        "source_anchor": deepcopy(source_anchor),
        "provenance": deepcopy(provenance or _provenance()),
        "exclusion": {
            "state": exclusion_state,
            "caused_by_item_ids": sorted(set(exclusion_causes)),
        },
        "omitted_reasons": sorted(set(omitted_reasons)),
    }


def _omission(
    layer: str,
    item_type: str,
    item_id: str,
    reason: str,
    priority: int,
    origin: str,
) -> dict[str, Any]:
    return {
        "layer": layer,
        "item_type": item_type,
        "item_id": item_id,
        "reason": reason,
        "priority": priority,
        "origin": origin,
    }


class _ContextDeliveryCore:
    """Private test seam; production still uses the fixed v7.3 service."""

    def __init__(self, *, context_assembly_core: Any) -> None:
        self.context_assembly_core = context_assembly_core

    def build(
        self,
        query: RetrievalQuery,
        *,
        budget_profile: str = "Medium",
        total_characters: int | None = None,
        estimated_tokens: int | None = None,
        max_evidence: int | None = None,
        max_candidates: int | None = None,
        max_history_expansions: int | None = None,
        excluded_item_ids: Iterable[str] = (),
        approved_profile: Iterable[dict[str, Any]] = (),
    ) -> dict[str, Any]:
        assembly = self.context_assembly_core.build(
            query,
            budget_profile=budget_profile,
            total_characters=total_characters,
            estimated_tokens=estimated_tokens,
            max_evidence=max_evidence,
            max_candidates=max_candidates,
            max_history_expansions=max_history_expansions,
            approved_profile=approved_profile,
        )
        delivery = build_context_delivery_from_assembly(
            assembly, excluded_item_ids=excluded_item_ids,
        )
        validate_context_delivery_against_assembly(delivery, trusted_assembly=assembly)
        return delivery


class ContextDeliveryService:
    """Fixed production service: no DB path, profile input, or provider injection."""

    def build(
        self,
        query: RetrievalQuery,
        *,
        budget_profile: str = "Medium",
        total_characters: int | None = None,
        estimated_tokens: int | None = None,
        max_evidence: int | None = None,
        max_candidates: int | None = None,
        max_history_expansions: int | None = None,
        excluded_item_ids: Iterable[str] = (),
    ) -> dict[str, Any]:
        assembly = ContextAssemblyService().build(
            query,
            budget_profile=budget_profile,
            total_characters=total_characters,
            estimated_tokens=estimated_tokens,
            max_evidence=max_evidence,
            max_candidates=max_candidates,
            max_history_expansions=max_history_expansions,
        )
        delivery = build_context_delivery_from_assembly(
            assembly, excluded_item_ids=excluded_item_ids,
        )
        validate_context_delivery_against_assembly(delivery, trusted_assembly=assembly)
        return delivery


def build_production_context_delivery(
    query: RetrievalQuery,
    *,
    budget_profile: str = "Medium",
    total_characters: int | None = None,
    estimated_tokens: int | None = None,
    max_evidence: int | None = None,
    max_candidates: int | None = None,
    max_history_expansions: int | None = None,
    excluded_item_ids: Iterable[str] = (),
) -> dict[str, Any]:
    return ContextDeliveryService().build(
        query,
        budget_profile=budget_profile,
        total_characters=total_characters,
        estimated_tokens=estimated_tokens,
        max_evidence=max_evidence,
        max_candidates=max_candidates,
        max_history_expansions=max_history_expansions,
        excluded_item_ids=excluded_item_ids,
    )


def build_context_delivery_from_assembly(
    assembly: dict[str, Any],
    *,
    excluded_item_ids: Iterable[str] = (),
) -> dict[str, Any]:
    validate_context_assembly_standalone(assembly)
    delivery = _build_delivery_payload(assembly, _normalize_exclusions(excluded_item_ids))
    validate_context_delivery_standalone(delivery)
    return delivery


def _build_delivery_payload(
    assembly: dict[str, Any],
    excluded_item_ids: list[str],
) -> dict[str, Any]:
    source_assembly = deepcopy(assembly)
    profile_source = source_assembly["confirmed_user_profile"]
    candidate_source = source_assembly["candidate_memory"]
    evidence_source = source_assembly["historical_evidence"]
    requested = set(excluded_item_ids)
    exclusion_closure = _resolve_exclusion_closure(source_assembly, excluded_item_ids)
    excluded = set(exclusion_closure["excluded_item_ids"])
    excluded_candidate_ids = set(exclusion_closure["excluded_candidate_ids"])
    excluded_evidence_ids = set(exclusion_closure["excluded_evidence_reference_ids"])
    excluded_relation_ids = set(exclusion_closure["excluded_relation_ids"])
    candidate_provenance, evidence_provenance, relation_provenance = _dependency_provenance(
        source_assembly
    )

    task_context = deepcopy(source_assembly["task_context"])
    profile_items = [
        deepcopy(item) for item in profile_source["items"]
        if item["profile_item_id"] not in excluded
    ]
    candidate_items = []
    for item in candidate_source["items"]:
        if item["candidate_id"] in excluded_candidate_ids:
            continue
        projected = deepcopy(item)
        projected["provenance"] = deepcopy(candidate_provenance[item["candidate_id"]])
        candidate_items.append(projected)
    candidate_ids = {item["candidate_id"] for item in candidate_items}
    candidate_relations = []
    for item in candidate_source["relations"]:
        if item["relation_id"] in excluded_relation_ids:
            continue
        if not {item["newer_candidate_id"], item["older_candidate_id"]} <= candidate_ids:
            raise ContextDeliveryValidationError("relation dependency closure is incomplete")
        projected = deepcopy(item)
        projected["provenance"] = deepcopy(relation_provenance[item["relation_id"]])
        candidate_relations.append(projected)
    evidence_items = []
    for item in evidence_source["items"]:
        if item["evidence_reference_id"] in excluded_evidence_ids:
            continue
        projected = deepcopy(item)
        provenance = deepcopy(evidence_provenance[item["evidence_reference_id"]])
        projected["provenance"] = provenance
        projected["expanded_excerpt_provenance"] = (
            deepcopy(provenance) if item["expanded_excerpt"] is not None else None
        )
        evidence_items.append(projected)
    task_context["current_retrieval_results"]["assembled_reference_ids"] = [
        item["evidence_reference_id"] for item in evidence_items
    ]

    final_profile = {
        "authority": profile_source["authority"],
        "production_input": profile_source["production_input"],
        "candidate_promotion_allowed": False,
        "items": profile_items,
    }
    final_candidates = {
        "candidate_only": True,
        "may_be_used_as_auxiliary_reference": True,
        "items": candidate_items,
        "relations": candidate_relations,
    }
    final_evidence = {
        "default_representation": evidence_source["default_representation"],
        "items": evidence_items,
        "expanded_count": sum(item["expanded_excerpt"] is not None for item in evidence_items),
    }
    model_context = _render_model_context(
        task_context, final_profile, final_candidates, final_evidence,
    )

    omissions = [
        _omission(
            item["layer"], item["item_type"], item["item_id"], item["reason"],
            item["priority"], "v7.3_assembly",
        )
        for item in source_assembly["omitted"]
    ]
    for item in profile_source["items"]:
        if item["profile_item_id"] in excluded:
            omissions.append(_omission(
                "approved_profile", "approved_profile", item["profile_item_id"],
                "user_excluded_current_delivery", 2, "v7.4_delivery",
            ))
    for item in candidate_source["items"]:
        if item["candidate_id"] in excluded_candidate_ids:
            omissions.append(_omission(
                "candidate_memory", "candidate", item["candidate_id"],
                (
                    "user_excluded_current_delivery"
                    if item["candidate_id"] in requested
                    else "provenance_excluded_current_delivery"
                ),
                3,
                "v7.4_delivery",
            ))
    for item in candidate_source["relations"]:
        if item["relation_id"] in excluded_relation_ids:
            omissions.append(_omission(
                "candidate_memory", "temporal_relation", item["relation_id"],
                "relation_dependency_excluded_current_delivery", 3, "v7.4_delivery",
            ))
    for item in evidence_source["items"]:
        if item["evidence_reference_id"] in excluded_evidence_ids:
            omissions.append(_omission(
                "historical_evidence", "evidence", item["evidence_reference_id"],
                (
                    "user_excluded_current_delivery"
                    if item["evidence_reference_id"] in requested
                    else "provenance_excluded_current_delivery"
                ),
                4,
                "v7.4_delivery",
            ))
    omissions.sort(key=lambda item: (
        item["priority"], item["layer"], item["item_type"], item["item_id"],
        item["reason"], item["origin"],
    ))

    reasons_by_id: dict[str, list[str]] = {}
    for item in omissions:
        reasons_by_id.setdefault(item["item_id"], []).append(item["reason"])
    review_items = [_review_item(
        layer="current_task",
        item_type="current_task",
        item_id="current_task",
        trust_status="current_request",
        included=True,
        excludable=False,
        content=task_context["current_user_question"],
        source_anchor=None,
    )]
    for item in profile_source["items"]:
        item_id = item["profile_item_id"]
        exclusion_state = "requested" if item_id in requested else "none"
        review_items.append(_review_item(
            layer="approved_profile",
            item_type="approved_profile",
            item_id=item_id,
            trust_status="approved",
            included=item_id not in excluded,
            excludable=True,
            content=item["statement"],
            source_anchor=None,
            exclusion_state=exclusion_state,
            exclusion_causes=exclusion_closure["causes_by_item_id"].get(item_id, ()),
            omitted_reasons=reasons_by_id.get(item_id, ()),
        ))
    for item in candidate_source["items"]:
        item_id = item["candidate_id"]
        exclusion_state = (
            "requested" if item_id in requested
            else "provenance_cascade" if item_id in excluded_candidate_ids
            else "none"
        )
        review_items.append(_review_item(
            layer="candidate_memory",
            item_type="candidate",
            item_id=item_id,
            trust_status="candidate/not_approved",
            included=item_id not in excluded_candidate_ids,
            excludable=True,
            content=item["statement"],
            source_anchor=_source_anchor_for_candidate(item),
            provenance=candidate_provenance[item_id],
            exclusion_state=exclusion_state,
            exclusion_causes=exclusion_closure["causes_by_item_id"].get(item_id, ()),
            omitted_reasons=reasons_by_id.get(item_id, ()),
        ))
    for item in candidate_source["relations"]:
        item_id = item["relation_id"]
        review_items.append(_review_item(
            layer="candidate_memory",
            item_type="temporal_relation",
            item_id=item_id,
            trust_status="candidate_relation/not_approved",
            included=item_id not in excluded_relation_ids,
            excludable=False,
            content=f"{item['newer_candidate_id']} -> {item['older_candidate_id']} ({item['relation']})",
            source_anchor=None,
            provenance=relation_provenance[item_id],
            exclusion_state=(
                "provenance_cascade" if item_id in excluded_relation_ids else "none"
            ),
            exclusion_causes=exclusion_closure["causes_by_item_id"].get(item_id, ()),
            omitted_reasons=reasons_by_id.get(item_id, ()),
        ))
    for item in evidence_source["items"]:
        item_id = item["evidence_reference_id"]
        content = item["expanded_excerpt"] or item["reference_uri"]
        exclusion_state = (
            "requested" if item_id in requested
            else "provenance_cascade" if item_id in excluded_evidence_ids
            else "none"
        )
        review_items.append(_review_item(
            layer="historical_evidence",
            item_type="evidence",
            item_id=item_id,
            trust_status="historical_evidence",
            included=item_id not in excluded_evidence_ids,
            excludable=True,
            content=content,
            source_anchor=_source_anchor_for_evidence(item),
            provenance=evidence_provenance[item_id],
            exclusion_state=exclusion_state,
            exclusion_causes=exclusion_closure["causes_by_item_id"].get(item_id, ()),
            omitted_reasons=reasons_by_id.get(item_id, ()),
        ))
    known_review_ids = {item["item_id"] for item in review_items}
    for item in omissions:
        if item["item_id"] not in known_review_ids:
            review_items.append(_review_item(
                layer=item["layer"],
                item_type=item["item_type"],
                item_id=item["item_id"],
                trust_status="omitted_before_delivery",
                included=False,
                excludable=False,
                content="",
                source_anchor=None,
                omitted_reasons=reasons_by_id[item["item_id"]],
            ))
            known_review_ids.add(item["item_id"])
    review_items.sort(key=lambda item: (
        {"current_task": 1, "approved_profile": 2, "confirmed_user_profile": 2,
         "candidate_memory": 3, "historical_evidence": 4}.get(item["layer"], 9),
        item["item_type"], item["item_id"],
    ))

    limits = source_assembly["selection_policy"]["limits"]
    budget = {
        "total_characters_limit": limits["total_characters"],
        "used_characters": len(model_context),
        "remaining_characters": limits["total_characters"] - len(model_context),
        "character_hard_limit_satisfied": len(model_context) <= limits["total_characters"],
        "estimated_token_limit": limits["estimated_tokens"],
        "estimated_tokens_used": estimate_tokens(model_context),
        "estimated_tokens_remaining": limits["estimated_tokens"] - estimate_tokens(model_context),
        "estimator_profile": ESTIMATOR_PROFILE,
        "exact_model_tokenizer": False,
    }
    capabilities = task_context["current_retrieval_results"]
    delivery = {
        "protocol_version": PROTOCOL_VERSION,
        "delivery_identity_version": DELIVERY_IDENTITY_VERSION,
        "delivery_identity": "",
        "assembly_binding": {
            "protocol_version": source_assembly["protocol_version"],
            "assembly_identity_version": source_assembly["assembly_identity_version"],
            "assembly_identity": source_assembly["assembly_identity"],
            "selection_policy_identity": source_assembly["selection_policy_identity"],
        },
        "controls": {
            "query": task_context["current_user_question"],
            "time_range": deepcopy(task_context["time_range"]),
            "budget_profile": source_assembly["selection_policy"]["budget_profile"],
            "budget_limits": deepcopy(limits),
            "temporary_exclusion_policy": TEMPORARY_EXCLUSION_POLICY,
            "excluded_item_ids": excluded_item_ids,
            "exclusion_closure": exclusion_closure,
        },
        "status": source_assembly["status"],
        "reason_code": source_assembly["reason_code"],
        "review_state": "customized" if excluded_item_ids else "unmodified",
        "execution": {
            "readonly": True,
            "persisted": False,
            "sent_to_model": False,
            "external_model_calls": False,
            "temporary_adjustments_only": True,
        },
        "capability_state": {
            "status": capabilities["status"],
            "reason_code": capabilities["reason_code"],
            "capabilities": deepcopy(capabilities["capabilities"]),
            "degradation": deepcopy(capabilities["degradation"]),
            "missing_capabilities": deepcopy(capabilities["missing_capabilities"]),
            "warnings": deepcopy(capabilities["warnings"]),
        },
        "layers": {
            "current_task": {
                "status": "included",
                "excludable": False,
                "content": task_context,
            },
            "approved_profile": {
                "status": "included" if profile_items else "empty",
                "empty_message": "No approved profile items are available for this request." if not profile_items else "",
                "authority": final_profile["authority"],
                "candidate_promotion_allowed": False,
                "items": profile_items,
            },
            "candidate_memory": final_candidates,
            "historical_evidence": final_evidence,
        },
        "omitted": omissions,
        "review_items": review_items,
        "counts": {
            "approved_profile_included": len(profile_items),
            "candidates_included": len(candidate_items),
            "candidate_relations_included": len(candidate_relations),
            "historical_evidence_included": len(evidence_items),
            "temporary_exclusion_requested": len(excluded_item_ids),
            "temporary_excluded": len(exclusion_closure["excluded_item_ids"]),
            "provenance_cascade_excluded": (
                len(exclusion_closure["excluded_item_ids"]) - len(excluded_item_ids)
            ),
            "relations_cascade_excluded": len(excluded_relation_ids),
            "omitted_manifest_entries": len(omissions),
            "review_items": len(review_items),
        },
        "budget": budget,
        "model_payload_provenance": _model_payload_provenance(
            candidate_items, candidate_relations, evidence_items,
        ),
        "model_context": model_context,
        "source_assembly": source_assembly,
    }
    delivery["delivery_identity"] = "dvy_" + _digest(_identity_payload(delivery))
    return delivery


def canonical_context_delivery_json(delivery: dict[str, Any]) -> str:
    validate_context_delivery_standalone(delivery)
    return _dump(delivery)


def render_context_delivery(delivery: dict[str, Any]) -> str:
    validate_context_delivery_standalone(delivery)
    budget = delivery["budget"]
    controls = delivery["controls"]
    lines = [
        f"YunOS Transparent Context Delivery {delivery['protocol_version']}",
        f"delivery_identity: {delivery['delivery_identity']}",
        f"assembly_identity: {delivery['assembly_binding']['assembly_identity']}",
        f"status: {delivery['status']} ({delivery['reason_code']})",
        f"review_state: {delivery['review_state']}",
        f"persisted: false",
        f"sent_to_model: false",
        f"budget_profile: {controls['budget_profile']}",
        f"time_range: start={controls['time_range']['start_time']}; end={controls['time_range']['end_time']}",
        f"characters: {budget['used_characters']}/{budget['total_characters_limit']}",
        f"estimated_tokens: {budget['estimated_tokens_used']}/{budget['estimated_token_limit']} ({budget['estimator_profile']}; not_model_exact)",
        "temporary_exclusion_requested: " + ",".join(controls["excluded_item_ids"] or ["none"]),
        "temporary_exclusion_closure: " + ",".join(
            controls["exclusion_closure"]["excluded_item_ids"] or ["none"]
        ),
        "omitted:",
    ]
    if delivery["omitted"]:
        lines.extend(
            f"- {item['layer']}/{item['item_type']}/{item['item_id']}: {item['reason']} ({item['origin']})"
            for item in delivery["omitted"]
        )
    else:
        lines.append("- none")
    lines.extend(("MODEL_CONTEXT_BEGIN", delivery["model_context"], "MODEL_CONTEXT_END"))
    return "\n".join(lines) + "\n"


def _validate_provenance(value: Any, label: str) -> None:
    if not isinstance(value, dict) or set(value) != {"source_ids", "evidence_ids", "event_ids"}:
        raise ContextDeliveryValidationError(f"{label} provenance fields mismatch")
    for kind in ("source_ids", "evidence_ids", "event_ids"):
        items = value[kind]
        if not isinstance(items, list) or items != sorted(set(items)):
            raise ContextDeliveryValidationError(f"{label} {kind} must be canonical")
        if any(not isinstance(item_id, str) or not item_id for item_id in items):
            raise ContextDeliveryValidationError(f"{label} {kind} contains an invalid identity")


def _validate_exclusion_closure_and_model_payload(
    delivery: dict[str, Any],
    exclusions: list[str],
) -> None:
    controls = delivery["controls"]
    expected_closure = _resolve_exclusion_closure(delivery["source_assembly"], exclusions)
    if controls.get("exclusion_closure") != expected_closure:
        raise ContextDeliveryValidationError("exclusion provenance closure mismatch")

    closure = controls["exclusion_closure"]
    excluded_candidates = set(closure["excluded_candidate_ids"])
    excluded_evidence = set(closure["excluded_evidence_reference_ids"])
    excluded_relations = set(closure["excluded_relation_ids"])
    candidates = delivery["layers"]["candidate_memory"]["items"]
    relations = delivery["layers"]["candidate_memory"]["relations"]
    evidence = delivery["layers"]["historical_evidence"]["items"]

    if excluded_candidates & {item["candidate_id"] for item in candidates}:
        raise ContextDeliveryValidationError("excluded candidate provenance reached model payload")
    if excluded_evidence & {item["evidence_reference_id"] for item in evidence}:
        raise ContextDeliveryValidationError("excluded evidence provenance reached model payload")
    if excluded_relations & {item["relation_id"] for item in relations}:
        raise ContextDeliveryValidationError("excluded relation provenance reached model payload")

    included_candidate_ids = {item["candidate_id"] for item in candidates}
    for item in candidates:
        _validate_provenance(item.get("provenance"), "candidate")
    for item in relations:
        _validate_provenance(item.get("provenance"), "relation")
        if not {item["newer_candidate_id"], item["older_candidate_id"]} <= included_candidate_ids:
            raise ContextDeliveryValidationError("relation has a dangling candidate dependency")
    for item in evidence:
        _validate_provenance(item.get("provenance"), "historical evidence")
        expanded = item.get("expanded_excerpt_provenance")
        if item["expanded_excerpt"] is None:
            if expanded is not None:
                raise ContextDeliveryValidationError("absent expansion declares provenance")
        else:
            _validate_provenance(expanded, "historical expansion")
            if expanded != item["provenance"]:
                raise ContextDeliveryValidationError("historical expansion provenance mismatch")

    actual_payload_provenance = _model_payload_provenance(candidates, relations, evidence)
    if delivery.get("model_payload_provenance") != actual_payload_provenance:
        raise ContextDeliveryValidationError("model payload provenance mismatch")
    _validate_provenance(actual_payload_provenance, "model payload")
    excluded_provenance = closure["excluded_provenance"]
    _validate_provenance(excluded_provenance, "excluded")
    for kind in ("source_ids", "evidence_ids", "event_ids"):
        if set(actual_payload_provenance[kind]) & set(excluded_provenance[kind]):
            raise ContextDeliveryValidationError(
                f"excluded {kind} reached the final model payload"
            )


def validate_context_delivery_standalone(delivery: dict[str, Any]) -> None:
    if not isinstance(delivery, dict):
        raise ContextDeliveryValidationError("context delivery must be an object")
    expected_keys = {
        "protocol_version", "delivery_identity_version", "delivery_identity",
        "assembly_binding", "controls", "status", "reason_code", "review_state",
        "execution", "capability_state", "layers", "omitted", "review_items",
        "counts", "budget", "model_payload_provenance", "model_context", "source_assembly",
    }
    if set(delivery) != expected_keys:
        raise ContextDeliveryValidationError("context delivery fields mismatch")
    if delivery["protocol_version"] != PROTOCOL_VERSION:
        raise ContextDeliveryValidationError("context delivery protocol mismatch")
    if delivery["delivery_identity_version"] != DELIVERY_IDENTITY_VERSION:
        raise ContextDeliveryValidationError("delivery identity version mismatch")
    if not re.fullmatch(r"dvy_[0-9a-f]{64}", str(delivery["delivery_identity"])):
        raise ContextDeliveryValidationError("invalid delivery identity")
    validate_context_assembly_standalone(delivery["source_assembly"])
    source = delivery["source_assembly"]
    expected_binding = {
        "protocol_version": source["protocol_version"],
        "assembly_identity_version": source["assembly_identity_version"],
        "assembly_identity": source["assembly_identity"],
        "selection_policy_identity": source["selection_policy_identity"],
    }
    if delivery["assembly_binding"] != expected_binding:
        raise ContextDeliveryValidationError("assembly binding mismatch")
    execution = delivery["execution"]
    if execution != {
        "readonly": True,
        "persisted": False,
        "sent_to_model": False,
        "external_model_calls": False,
        "temporary_adjustments_only": True,
    }:
        raise ContextDeliveryValidationError("delivery execution boundary mismatch")
    exclusions = _normalize_exclusions(delivery["controls"]["excluded_item_ids"])
    if exclusions != delivery["controls"]["excluded_item_ids"]:
        raise ContextDeliveryValidationError("excluded item identities are not canonical")
    _validate_exclusion_closure_and_model_payload(delivery, exclusions)
    rebuilt = _build_delivery_payload(source, exclusions)
    if rebuilt != delivery:
        raise ContextDeliveryValidationError("context delivery reconstruction mismatch")
    if delivery["delivery_identity"] != "dvy_" + _digest(_identity_payload(delivery)):
        raise ContextDeliveryValidationError("delivery identity mismatch")


def validate_context_delivery_against_assembly(
    delivery: dict[str, Any],
    *,
    trusted_assembly: dict[str, Any],
) -> None:
    validate_context_delivery_standalone(delivery)
    validate_context_assembly_standalone(trusted_assembly)
    if delivery["source_assembly"] != trusted_assembly:
        raise ContextDeliveryValidationError("trusted context assembly mismatch")
    rebuilt = _build_delivery_payload(
        trusted_assembly, delivery["controls"]["excluded_item_ids"],
    )
    if rebuilt != delivery:
        raise ContextDeliveryValidationError("trusted assembly delivery reconstruction mismatch")


__all__ = [
    "PROTOCOL_VERSION",
    "DELIVERY_IDENTITY_VERSION",
    "TEMPORARY_EXCLUSION_POLICY",
    "ContextDeliveryValidationError",
    "ContextDeliveryService",
    "_ContextDeliveryCore",
    "build_production_context_delivery",
    "build_context_delivery_from_assembly",
    "canonical_context_delivery_json",
    "render_context_delivery",
    "validate_context_delivery_standalone",
    "validate_context_delivery_against_assembly",
]
