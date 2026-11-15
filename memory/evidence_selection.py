"""YunOS v7.1 deterministic evidence selection and budgeting.

This read-only layer consumes the immutable v7.0 ``v7-memory-package-1``
contract.  It performs only structural anchor merging, exact deterministic
deduplication, stable selection, and reproducible budgeting.  It never calls a
model, rewrites evidence, or persists a result.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import unicodedata
from typing import Any

from memory.db_path_policy import DEFAULT_REAL_DB_PATH
from memory.graph_enhanced_retrieval import (
    RetrievalQuery,
    RetrievalValidationError,
    expected_retrieval_capabilities,
    project_retrieval_evidence_state,
    validate_retrieval_envelope_state,
)
from memory.memory_package import (
    MemoryPackageService,
    MemoryPackageValidationError,
    PROTOCOL_VERSION as SOURCE_PROTOCOL_VERSION,
    _reject_absolute_paths as _reject_v70_absolute_paths,
    _validate_capabilities as _validate_v70_capabilities,
    _validate_degradation as _validate_v70_degradation,
    _validate_evidence_details as _validate_v70_evidence_details,
    _validate_request as _validate_v70_request,
    _validate_retrieval as _validate_v70_retrieval,
    _validate_warnings as _validate_v70_warnings,
    validate_memory_package_source_backed,
    validate_memory_package_standalone,
)


PROTOCOL_VERSION = "v7.1-evidence-selection-2"
POLICY_VERSION = "v7.1-selection-policy-2"
ESTIMATOR_PROFILE = "unicode-codepoint-ceil-div4-v1"
EXCERPT_COMMITMENT_PROFILE = "sha256-utf8-and-nfc-newline-v1"
DUPLICATE_EQUIVALENCE_PROFILE = "role-nfc-newline-sha256-v1"
OMISSION_REASONS = ("duplicate", "conversation_cap", "budget_exceeded", "lower_priority")
HIT_RANK = {"keyword": 0, "semantic": 1, "graph": 2}
PACKAGE_STATUSES = {"ok", "partial", "empty", "unavailable"}


@dataclass(frozen=True)
class BudgetProfile:
    name: str
    character_budget: int
    estimated_token_budget: int
    per_conversation_cap: int
    max_selected: int


BUILTIN_PROFILES = {
    "small": BudgetProfile("Small", 2_000, 500, 2, 6),
    "medium": BudgetProfile("Medium", 6_000, 1_500, 3, 16),
    "large": BudgetProfile("Large", 16_000, 4_000, 5, 40),
}
CUSTOM_LIMITS = {
    "character_budget": (256, 100_000),
    "estimated_token_budget": (64, 25_000),
    "per_conversation_cap": (1, 20),
    "max_selected": (1, 100),
}


class EvidenceSelectionValidationError(ValueError):
    """The v7.1 package or policy is malformed or cannot be reproduced."""


class EvidenceSelectionService:
    """Build v7.1 selections without accepting a persistence destination."""

    def __init__(
        self,
        source_db: str | Path = DEFAULT_REAL_DB_PATH,
        *,
        memory_package_service: MemoryPackageService | None = None,
    ) -> None:
        self.source_db = Path(source_db).expanduser().resolve()
        self.memory_package_service = memory_package_service or MemoryPackageService(self.source_db)

    def build(
        self,
        query: RetrievalQuery,
        *,
        budget_profile: str = "Medium",
        character_budget: int | None = None,
        estimated_token_budget: int | None = None,
        per_conversation_cap: int | None = None,
        max_selected: int | None = None,
    ) -> dict[str, Any]:
        policy = normalize_policy(
            budget_profile,
            character_budget=character_budget,
            estimated_token_budget=estimated_token_budget,
            per_conversation_cap=per_conversation_cap,
            max_selected=max_selected,
        )
        source_package = self.memory_package_service.build(query)
        package = build_selection_from_memory_package(source_package, policy=policy)
        validate_evidence_selection_source_backed(
            package,
            trusted_source_package=source_package,
            trusted_source_db=self.source_db,
        )
        return package


def build_production_evidence_selection(
    query: RetrievalQuery,
    *,
    budget_profile: str = "Medium",
    character_budget: int | None = None,
    estimated_token_budget: int | None = None,
    per_conversation_cap: int | None = None,
    max_selected: int | None = None,
) -> dict[str, Any]:
    """Build only from the fixed production source path in read-only mode."""
    return EvidenceSelectionService().build(
        query,
        budget_profile=budget_profile,
        character_budget=character_budget,
        estimated_token_budget=estimated_token_budget,
        per_conversation_cap=per_conversation_cap,
        max_selected=max_selected,
    )


def normalize_policy(
    budget_profile: str,
    *,
    character_budget: int | None = None,
    estimated_token_budget: int | None = None,
    per_conversation_cap: int | None = None,
    max_selected: int | None = None,
) -> dict[str, Any]:
    if not isinstance(budget_profile, str):
        raise EvidenceSelectionValidationError("budget_profile must be Small, Medium, Large, or Custom")
    key = budget_profile.strip().casefold()
    if key in BUILTIN_PROFILES:
        if any(value is not None for value in (
            character_budget, estimated_token_budget, per_conversation_cap, max_selected,
        )):
            raise EvidenceSelectionValidationError("built-in budget profiles do not accept overrides")
        profile = BUILTIN_PROFILES[key]
    elif key == "custom":
        values = {
            "character_budget": character_budget,
            "estimated_token_budget": estimated_token_budget,
            "per_conversation_cap": per_conversation_cap,
            "max_selected": max_selected,
        }
        for name, value in values.items():
            low, high = CUSTOM_LIMITS[name]
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise EvidenceSelectionValidationError(
                    f"Custom {name} must be an integer between {low} and {high}"
                )
        profile = BudgetProfile(
            "Custom", character_budget, estimated_token_budget,
            per_conversation_cap, max_selected,
        )
    else:
        raise EvidenceSelectionValidationError("budget_profile must be Small, Medium, Large, or Custom")
    return {
        "policy_version": POLICY_VERSION,
        "budget_profile": profile.name,
        "character_budget": profile.character_budget,
        "estimated_token_budget": profile.estimated_token_budget,
        "per_conversation_cap": profile.per_conversation_cap,
        "max_selected": profile.max_selected,
        "priority_order": ["direct", "graph_only"],
        "direct_hit_types": ["keyword", "semantic"],
        "diversity_strategy": "priority_tier_conversation_round_robin_v1",
        "deduplication_rules": [
            "same_source_anchor_merge_all_hit_provenance",
            "nfc_and_newline_normalized_exact_role_excerpt_equivalence",
        ],
        "token_estimator": {
            "profile": ESTIMATOR_PROFILE,
            "method": "ceil(unicode_code_points/4)",
            "unit": "estimated_tokens",
            "exact_model_tokenizer": False,
        },
    }


def build_selection_from_memory_package(
    source_package: dict[str, Any], *, policy: dict[str, Any],
) -> dict[str, Any]:
    """Build a deterministic selection from an already validated v7.0 package."""
    validate_memory_package_standalone(source_package)
    normalized_policy = _validate_policy(policy)
    anchors = _merge_source_anchors(source_package["evidence"])
    representatives = _deduplicate_anchors(anchors)
    ledger = [_ledger_entry(candidate) for candidate in anchors]
    ordered = _diversified_order(representatives)
    selected, selection_omissions, model_payload = _select(ordered, normalized_policy)
    duplicate_omissions = [
        _omitted(candidate, "duplicate", duplicate_of=candidate["duplicate_equivalence"]["representative_candidate_id"])
        for candidate in anchors
        if candidate["duplicate_equivalence"]["representative_candidate_id"] != candidate["candidate_id"]
    ]
    omitted = sorted(duplicate_omissions + selection_omissions, key=lambda item: item["candidate_id"])
    source_status = source_package["status"]
    if source_status == "unavailable":
        status, reason = "unavailable", source_package["reason_code"]
    elif not source_package["evidence"]:
        status, reason = "empty", "no_evidence"
    elif not selected:
        status, reason = "empty", "budget_excluded_all"
    elif source_status == "partial":
        status, reason = "partial", "partial_capability_selection"
    else:
        status, reason = "ok", "selection_complete"
    used_chars = len(model_payload)
    estimated_tokens = estimate_tokens(model_payload)
    reason_counts = {reason: 0 for reason in OMISSION_REASONS}
    for item in omitted:
        reason_counts[item["reason"]] += 1
    package = {
        "protocol_version": PROTOCOL_VERSION,
        "package_identity": "",
        "status": status,
        "reason_code": reason,
        "readonly": True,
        "persisted": False,
        "source_projection": _source_projection(source_package),
        "request": deepcopy(source_package["request"]),
        "capabilities": deepcopy(source_package["capabilities"]),
        "degradation": deepcopy(source_package["degradation"]),
        "warnings": deepcopy(source_package["warnings"]),
        "missing_capabilities": deepcopy(source_package["missing_capabilities"]),
        "selection_policy": deepcopy(normalized_policy),
        "candidate_ledger": ledger,
        "selected": selected,
        "omitted": omitted,
        "counts": {
            "input_evidence": len(source_package["evidence"]),
            "merged_anchors": len(ledger),
            "deduplicated_candidates": len(representatives),
            "selected": len(selected),
            "omitted": len(omitted),
            "omission_reasons": reason_counts,
            "selected_conversations": len({
                item["source"]["conversation"]["conversation_id"] for item in selected
            }),
        },
        "budget": {
            "character_budget": normalized_policy["character_budget"],
            "used_characters": used_chars,
            "remaining_characters": normalized_policy["character_budget"] - used_chars,
            "character_hard_limit_satisfied": used_chars <= normalized_policy["character_budget"],
            "estimated_token_budget": normalized_policy["estimated_token_budget"],
            "estimated_tokens_used": estimated_tokens,
            "estimated_tokens_remaining": normalized_policy["estimated_token_budget"] - estimated_tokens,
            "estimator_profile": ESTIMATOR_PROFILE,
            "exact_model_tokenizer": False,
        },
        "model_payload": model_payload,
    }
    package["package_identity"] = "msp_" + _digest(_identity_payload(package))
    validate_evidence_selection_standalone(package)
    return package


def canonical_selection_json(package: dict[str, Any]) -> str:
    validate_evidence_selection_standalone(package)
    return _canonical_dump(package)


def render_evidence_selection(package: dict[str, Any]) -> str:
    """Render the same selected model payload plus deterministic selection metadata."""
    validate_evidence_selection_standalone(package)
    budget = package["budget"]
    counts = package["counts"]
    lines = [
        f"YunOS Evidence Selection {package['protocol_version']}",
        f"package_identity: {package['package_identity']}",
        f"source_package_identity: {package['source_projection']['package_identity']}",
        f"status: {package['status']} ({package['reason_code']})",
        f"policy: {package['selection_policy']['policy_version']} / {package['selection_policy']['budget_profile']}",
        f"selected: {counts['selected']}; omitted: {counts['omitted']}; conversations: {counts['selected_conversations']}",
        f"characters: {budget['used_characters']}/{budget['character_budget']} (hard_limit=true)",
        f"estimated_tokens: {budget['estimated_tokens_used']}/{budget['estimated_token_budget']} ({budget['estimator_profile']}; not_model_exact)",
        "omission_reasons: " + ",".join(
            f"{name}={counts['omission_reasons'][name]}" for name in OMISSION_REASONS
        ),
        "MODEL_PAYLOAD_BEGIN",
        package["model_payload"],
        "MODEL_PAYLOAD_END",
    ]
    return "\n".join(lines) + "\n"


def validate_evidence_selection_standalone(package: dict[str, Any]) -> None:
    """Validate only package-internal consistency; this is not source authenticity."""
    if not isinstance(package, dict):
        raise EvidenceSelectionValidationError("selection package must be an object")
    _exact_keys(package, {
        "protocol_version", "package_identity", "status", "reason_code", "readonly", "persisted",
        "source_projection", "request", "capabilities", "degradation", "warnings",
        "missing_capabilities", "selection_policy", "candidate_ledger", "selected",
        "omitted", "counts", "budget", "model_payload",
    }, "selection package")
    if package["protocol_version"] != PROTOCOL_VERSION:
        raise EvidenceSelectionValidationError("selection protocol version mismatch")
    if package["status"] not in PACKAGE_STATUSES or not _token(package["reason_code"]):
        raise EvidenceSelectionValidationError("invalid selection status")
    if package["readonly"] is not True or package["persisted"] is not False:
        raise EvidenceSelectionValidationError("selection package must be readonly and non-persisted")
    policy = _validate_policy(package["selection_policy"])
    ledger = _validate_candidate_ledger(package["candidate_ledger"])
    source = _validate_source_projection(package["source_projection"], ledger)
    if package["request"] != source["request"]:
        raise EvidenceSelectionValidationError("request does not match the source projection")
    for field in ("capabilities", "degradation", "warnings", "missing_capabilities"):
        if package[field] != source[field]:
            raise EvidenceSelectionValidationError(f"{field} does not match the source projection")
    if not isinstance(package["selected"], list) or not isinstance(package["omitted"], list):
        raise EvidenceSelectionValidationError("selected and omitted must be lists")
    selected_by_id = _validate_selected_manifest(package["selected"], ledger)
    representatives = [
        item for item in ledger
        if item["duplicate_equivalence"]["representative_candidate_id"] == item["candidate_id"]
    ]
    ordered = _diversified_order(representatives)
    selected_stubs, selection_omissions = _selection_plan(ordered, policy)
    expected_selected_ids = [item["candidate_id"] for item in selected_stubs]
    if list(selected_by_id) != expected_selected_ids:
        raise EvidenceSelectionValidationError("selected list does not match deterministic selection")
    expected_omitted = [
        _omitted(
            item, "duplicate",
            duplicate_of=item["duplicate_equivalence"]["representative_candidate_id"],
        )
        for item in ledger
        if item["duplicate_equivalence"]["representative_candidate_id"] != item["candidate_id"]
    ] + selection_omissions
    expected_omitted.sort(key=lambda item: item["candidate_id"])
    omitted = [_validate_omitted(item) for item in package["omitted"]]
    if omitted != expected_omitted:
        raise EvidenceSelectionValidationError("omitted manifest does not match the canonical ledger")
    selected = [selected_by_id[candidate_id] for candidate_id in expected_selected_ids]
    payload = _materialize_payload(selected)
    if package["model_payload"] != payload:
        raise EvidenceSelectionValidationError("model payload does not match selected evidence")
    _validate_counts(package, ledger, selected, omitted, source)
    _validate_budget(package, policy, payload)
    expected_status = _selection_status(
        source["status"], source["reason_code"], bool(selected), bool(ledger),
    )
    if (package["status"], package["reason_code"]) != expected_status:
        raise EvidenceSelectionValidationError("selection status propagation mismatch")
    expected_identity = "msp_" + _digest(_identity_payload(package))
    if package["package_identity"] != expected_identity:
        raise EvidenceSelectionValidationError("selection package identity mismatch")
    try:
        _reject_v70_absolute_paths(package)
    except MemoryPackageValidationError as exc:
        raise EvidenceSelectionValidationError("selection package violates the v7.0 privacy boundary") from exc


def validate_evidence_selection_source_backed(
    package: dict[str, Any], *, trusted_source_package: dict[str, Any], trusted_source_db: str | Path,
) -> None:
    """Authenticate v7.0 from readonly SQLite and reproduce the complete v7.1 result."""
    validate_evidence_selection_standalone(package)
    validate_memory_package_source_backed(
        trusted_source_package, trusted_source_db=trusted_source_db,
    )
    if package["source_projection"]["package_identity"] != trusted_source_package["package_identity"]:
        raise EvidenceSelectionValidationError("trusted source package identity mismatch")
    rebuilt = build_selection_from_memory_package(
        trusted_source_package, policy=package["selection_policy"],
    )
    if package != rebuilt:
        raise EvidenceSelectionValidationError("source-backed selection reconstruction mismatch")


def estimate_tokens(text: str) -> int:
    """Return a model-independent estimate; this is not a tokenizer result."""
    return math.ceil(len(text) / 4)


def _source_projection(source_package: dict[str, Any]) -> dict[str, Any]:
    return {
        "protocol_version": source_package["protocol_version"],
        "package_identity": source_package["package_identity"],
        "status": source_package["status"],
        "reason_code": source_package["reason_code"],
        "readonly": source_package["readonly"],
        "persisted": source_package["persisted"],
        "request": deepcopy(source_package["request"]),
        "retrieval": deepcopy(source_package["retrieval"]),
        "capabilities": deepcopy(source_package["capabilities"]),
        "degradation": deepcopy(source_package["degradation"]),
        "warnings": deepcopy(source_package["warnings"]),
        "missing_capabilities": deepcopy(source_package["missing_capabilities"]),
        "evidence_order": [item["evidence_id"] for item in source_package["evidence"]],
        "evidence_count": len(source_package["evidence"]),
    }


def _normalized_excerpt(excerpt: str) -> str:
    return unicodedata.normalize("NFC", excerpt.replace("\r\n", "\n").replace("\r", "\n"))


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _excerpt_commitment(excerpt: str) -> dict[str, Any]:
    normalized = _normalized_excerpt(excerpt)
    return {
        "profile": EXCERPT_COMMITMENT_PROFILE,
        "sha256": _text_sha256(excerpt),
        "unicode_codepoints": len(excerpt),
        "normalized_sha256": _text_sha256(normalized),
        "normalized_unicode_codepoints": len(normalized),
    }


def _candidate_identity(candidate: dict[str, Any]) -> str:
    return "cand_" + _digest({
        "source": candidate["source"],
        "excerpt_commitment": candidate["excerpt_commitment"],
        "hit_provenance": candidate["hit_provenance"],
    })


def _duplicate_equivalence_key(candidate: dict[str, Any]) -> str:
    return "eq_" + _digest({
        "profile": DUPLICATE_EQUIVALENCE_PROFILE,
        "role": candidate["source"]["message_anchor"]["role"],
        "normalized_excerpt_sha256": candidate["excerpt_commitment"]["normalized_sha256"],
        "normalized_unicode_codepoints": candidate["excerpt_commitment"]["normalized_unicode_codepoints"],
    })


def _duplicate_source(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate_id": candidate["candidate_id"],
        "conversation_id": candidate["source"]["conversation"]["conversation_id"],
        "message_id": candidate["source"]["message_anchor"]["message_id"],
        "reference_uri": candidate["source"]["reference_uri"],
        "evidence_ids": deepcopy(candidate["evidence_ids"]),
        "hit_provenance": deepcopy(candidate["hit_provenance"]),
    }


def _ledger_entry(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate_id": candidate["candidate_id"],
        "source": deepcopy(candidate["source"]),
        "excerpt_commitment": deepcopy(candidate["excerpt_commitment"]),
        "evidence_ids": deepcopy(candidate["evidence_ids"]),
        "hit_provenance": deepcopy(candidate["hit_provenance"]),
        "priority": deepcopy(candidate["priority"]),
        "duplicate_equivalence": deepcopy(candidate["duplicate_equivalence"]),
        "render_cost": {
            "payload_chars": candidate["payload_chars"],
            "estimated_tokens": candidate["estimated_tokens"],
        },
    }


def _selected_manifest_item(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate_id": candidate["candidate_id"],
        "source": deepcopy(candidate["source"]),
        "excerpt": candidate["excerpt"],
        "evidence_ids": deepcopy(candidate["evidence_ids"]),
        "hit_provenance": deepcopy(candidate["hit_provenance"]),
        "duplicate_sources": deepcopy(candidate["duplicate_sources"]),
        "priority": deepcopy(candidate["priority"]),
        "payload_chars": candidate["payload_chars"],
        "estimated_tokens": candidate["estimated_tokens"],
    }


def _merge_source_anchors(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for item in evidence:
        conversation_id = item["source"]["conversation"]["conversation_id"]
        message_id = item["source"]["message_anchor"]["message_id"]
        key = (conversation_id, message_id)
        if key not in merged:
            merged[key] = {
                "source": deepcopy(item["source"]),
                "excerpt": item["excerpt"],
                "evidence_ids": [],
                "hit_provenance": [],
            }
        current = merged[key]
        if current["excerpt"] != item["excerpt"] or current["source"] != item["source"]:
            raise EvidenceSelectionValidationError("same anchor has inconsistent v7.0 source projection")
        current["evidence_ids"].append(item["evidence_id"])
        current["hit_provenance"].append({
            "evidence_id": item["evidence_id"],
            "result_rank": item["result_rank"],
            "hit_type": item["hit"]["type"],
            "score": item["hit"]["score"],
            "basis_code": item["explanation"]["basis_code"],
            "explanation": item["explanation"]["text"],
            "details": deepcopy(item["details"]),
        })
    result = []
    for value in merged.values():
        value["hit_provenance"].sort(key=lambda hit: (
            HIT_RANK[hit["hit_type"]], hit["result_rank"], -float(hit["score"]), hit["evidence_id"],
        ))
        value["evidence_ids"] = [hit["evidence_id"] for hit in value["hit_provenance"]]
        value["duplicate_sources"] = []
        value["excerpt_commitment"] = _excerpt_commitment(value["excerpt"])
        value["candidate_id"] = _candidate_identity(value)
        value["priority"] = _priority(value)
        value["duplicate_equivalence"] = {
            "profile": DUPLICATE_EQUIVALENCE_PROFILE,
            "key": _duplicate_equivalence_key(value),
            "representative_candidate_id": "",
        }
        result.append(value)
    return sorted(result, key=_candidate_sort_key)


def _deduplicate_anchors(
    anchors: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    representatives: list[dict[str, Any]] = []
    by_equivalence: dict[str, dict[str, Any]] = {}
    for candidate in anchors:
        equivalence = candidate["duplicate_equivalence"]["key"]
        if equivalence not in by_equivalence:
            by_equivalence[equivalence] = candidate
            representatives.append(candidate)
        representative = by_equivalence[equivalence]
        candidate["duplicate_equivalence"]["representative_candidate_id"] = representative["candidate_id"]
        if candidate is not representative:
            representative["duplicate_sources"].append(_duplicate_source(candidate))
    for representative in representatives:
        representative["duplicate_sources"].sort(key=lambda item: item["candidate_id"])
    for candidate in anchors:
        fragment = _render_candidate(candidate)
        candidate["payload_chars"] = len(fragment)
        candidate["estimated_tokens"] = estimate_tokens(fragment)
    return representatives


def _diversified_order(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered: list[dict[str, Any]] = []
    for tier in (0, 1):
        tier_candidates = sorted(
            (item for item in candidates if item["priority"]["tier"] == tier),
            key=_candidate_sort_key,
        )
        by_conversation: dict[str, list[dict[str, Any]]] = {}
        conversation_order: list[str] = []
        for item in tier_candidates:
            conversation_id = _conversation_id(item)
            if conversation_id not in by_conversation:
                by_conversation[conversation_id] = []
                conversation_order.append(conversation_id)
            by_conversation[conversation_id].append(item)
        round_index = 0
        while True:
            added = False
            for conversation_id in conversation_order:
                bucket = by_conversation[conversation_id]
                if round_index < len(bucket):
                    ordered.append(bucket[round_index])
                    added = True
            if not added:
                break
            round_index += 1
    return ordered


def _select(
    ordered: list[dict[str, Any]], policy: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    selected_stubs, omitted = _selection_plan(ordered, policy)
    selected = [_selected_manifest_item(item) for item in selected_stubs]
    return selected, omitted, _materialize_payload(selected)


def _selection_plan(
    ordered: list[dict[str, Any]], policy: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    selected: list[dict[str, Any]] = []
    omitted: list[dict[str, Any]] = []
    selected_per_conversation: dict[str, int] = {}
    used_chars = len(_payload_header())
    direct_candidates = [item for item in ordered if item["priority"]["class"] == "direct"]
    graph_candidates = [item for item in ordered if item["priority"]["class"] == "graph_only"]
    direct_incomplete = False

    def consider(candidate: dict[str, Any]) -> str | None:
        nonlocal used_chars
        conversation_id = _conversation_id(candidate)
        if selected_per_conversation.get(conversation_id, 0) >= policy["per_conversation_cap"]:
            return "conversation_cap"
        if len(selected) >= policy["max_selected"]:
            return "lower_priority"
        candidate_chars = _render_cost(candidate)["payload_chars"]
        separator = "\n" if selected else ""
        proposed_chars = used_chars + len(separator) + candidate_chars
        proposed_tokens = math.ceil(proposed_chars / 4)
        if (
            proposed_chars > policy["character_budget"]
            or proposed_tokens > policy["estimated_token_budget"]
        ):
            return "budget_exceeded"
        used_chars = proposed_chars
        selected.append(candidate)
        selected_per_conversation[conversation_id] = selected_per_conversation.get(conversation_id, 0) + 1
        return None

    for candidate in direct_candidates:
        reason = consider(candidate)
        if reason is not None:
            direct_incomplete = True
            omitted.append(_omitted(candidate, reason))
    for candidate in graph_candidates:
        if direct_incomplete:
            omitted.append(_omitted(candidate, "lower_priority"))
            continue
        reason = consider(candidate)
        if reason is not None:
            omitted.append(_omitted(candidate, reason))
    return selected, omitted


def _materialize_payload(selected: list[dict[str, Any]]) -> str:
    payload = _payload_header()
    for candidate in selected:
        if "excerpt" not in candidate:
            raise EvidenceSelectionValidationError("selected candidate body is unavailable")
        payload += ("\n" if payload != _payload_header() else "") + _render_candidate(candidate)
    return payload


def _render_candidate(candidate: dict[str, Any]) -> str:
    source = candidate["source"]
    conversation = source["conversation"]
    anchor = source["message_anchor"]
    hit_summary = ",".join(
        f"{hit['hit_type']}:{hit['evidence_id']}:{hit['score']}" for hit in candidate["hit_provenance"]
    )
    duplicate_summary = ",".join(
        f"{item['candidate_id']}@{item['conversation_id']}/{item['message_id']}"
        for item in candidate.get("duplicate_sources", [])
    ) or "none"
    return "\n".join([
        f"[{candidate['candidate_id']}]",
        f"conversation: {conversation['conversation_id']} | {conversation['title']}",
        f"anchor: {anchor['message_id']} | {anchor['role']} | {anchor['time']}",
        f"source: {source['reference_uri']}",
        f"hits: {hit_summary}",
        f"exact_duplicate_sources: {duplicate_summary}",
        f"excerpt: {candidate['excerpt']}",
    ])


def _payload_header() -> str:
    return f"YunOS memory evidence {PROTOCOL_VERSION}\n"


def _render_cost(candidate: dict[str, Any]) -> dict[str, int]:
    if "render_cost" in candidate:
        return candidate["render_cost"]
    return {
        "payload_chars": candidate["payload_chars"],
        "estimated_tokens": candidate["estimated_tokens"],
    }


def _priority(candidate: dict[str, Any]) -> dict[str, Any]:
    hits = candidate["hit_provenance"]
    direct = any(hit["hit_type"] in {"keyword", "semantic"} for hit in hits)
    best = min(hits, key=lambda hit: (
        HIT_RANK[hit["hit_type"]], hit["result_rank"], -float(hit["score"]), hit["evidence_id"],
    ))
    return {
        "tier": 0 if direct else 1,
        "class": "direct" if direct else "graph_only",
        "best_hit_type": best["hit_type"],
        "best_result_rank": min(hit["result_rank"] for hit in hits),
        "best_score": max(float(hit["score"]) for hit in hits),
    }


def _candidate_sort_key(candidate: dict[str, Any]) -> tuple[Any, ...]:
    priority = candidate["priority"]
    return (
        priority["tier"], priority["best_result_rank"],
        HIT_RANK[priority["best_hit_type"]], -float(priority["best_score"]),
        _conversation_id(candidate), _message_id(candidate), candidate["candidate_id"],
    )


def _omitted(
    candidate: dict[str, Any], reason: str, *, duplicate_of: str | None = None,
) -> dict[str, Any]:
    source = candidate["source"] if "source" in candidate else None
    if source is None:
        source_anchor = deepcopy(candidate["source_anchor"])
    else:
        source_anchor = {
            "conversation_id": source["conversation"]["conversation_id"],
            "message_id": source["message_anchor"]["message_id"],
            "reference_uri": source["reference_uri"],
            "role": source["message_anchor"]["role"],
            "time": source["message_anchor"]["time"],
            "timestamp": source["message_anchor"]["timestamp"],
        }
    cost = _render_cost(candidate)
    return {
        "candidate_id": candidate["candidate_id"],
        "evidence_ids": deepcopy(candidate["evidence_ids"]),
        "source_anchor": source_anchor,
        "reason": reason,
        "duplicate_of": duplicate_of,
        "priority": deepcopy(candidate["priority"]),
        "payload_chars": cost["payload_chars"],
        "estimated_tokens": cost["estimated_tokens"],
        # Provenance is not body text. Keep the complete v7 hit explanation and
        # graph seed/path details even when the excerpt itself is excluded.
        "hit_provenance": deepcopy(candidate["hit_provenance"]),
    }


def _validate_policy(policy: Any) -> dict[str, Any]:
    if not isinstance(policy, dict):
        raise EvidenceSelectionValidationError("selection_policy must be an object")
    _exact_keys(policy, {
        "policy_version", "budget_profile", "character_budget", "estimated_token_budget",
        "per_conversation_cap", "max_selected", "priority_order", "direct_hit_types",
        "diversity_strategy", "deduplication_rules", "token_estimator",
    }, "selection_policy")
    expected = normalize_policy(
        policy["budget_profile"],
        **({
            "character_budget": policy["character_budget"],
            "estimated_token_budget": policy["estimated_token_budget"],
            "per_conversation_cap": policy["per_conversation_cap"],
            "max_selected": policy["max_selected"],
        } if policy["budget_profile"] == "Custom" else {}),
    )
    if policy != expected:
        raise EvidenceSelectionValidationError("selection policy does not match its versioned profile")
    return deepcopy(expected)


def _validate_candidate_ledger(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise EvidenceSelectionValidationError("candidate_ledger must be a list")
    ledger = [_validate_ledger_entry(item) for item in value]
    candidate_ids = [item["candidate_id"] for item in ledger]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise EvidenceSelectionValidationError("candidate ledger identities are not unique")
    if ledger != sorted(ledger, key=_candidate_sort_key):
        raise EvidenceSelectionValidationError("candidate ledger is not canonically ordered")
    evidence_ids = [evidence_id for item in ledger for evidence_id in item["evidence_ids"]]
    if len(evidence_ids) != len(set(evidence_ids)):
        raise EvidenceSelectionValidationError("candidate ledger evidence identities are not unique")

    representatives: dict[str, dict[str, Any]] = {}
    for item in ledger:
        key = item["duplicate_equivalence"]["key"]
        representatives.setdefault(key, item)
        expected = representatives[key]["candidate_id"]
        if item["duplicate_equivalence"]["representative_candidate_id"] != expected:
            raise EvidenceSelectionValidationError("duplicate representative is not canonical")
    for item in ledger:
        key = item["duplicate_equivalence"]["key"]
        representative = representatives[key]
        duplicates = [
            _duplicate_source(candidate) for candidate in ledger
            if candidate["duplicate_equivalence"]["key"] == key
            and candidate["candidate_id"] != representative["candidate_id"]
        ] if item["candidate_id"] == representative["candidate_id"] else []
        duplicates.sort(key=lambda entry: entry["candidate_id"])
        item["_derived_duplicate_sources"] = duplicates
        expected_cost = _expected_render_cost(item, duplicates)
        if item["render_cost"] != expected_cost:
            raise EvidenceSelectionValidationError("candidate ledger render cost mismatch")
    return ledger


def _validate_ledger_entry(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise EvidenceSelectionValidationError("candidate ledger entry must be an object")
    _exact_keys(item, {
        "candidate_id", "source", "excerpt_commitment", "evidence_ids",
        "hit_provenance", "priority", "duplicate_equivalence", "render_cost",
    }, "candidate ledger entry")
    candidate = deepcopy(item)
    _validate_source(candidate["source"], "candidate ledger source")
    commitment = candidate["excerpt_commitment"]
    _exact_keys(commitment, {
        "profile", "sha256", "unicode_codepoints", "normalized_sha256",
        "normalized_unicode_codepoints",
    }, "excerpt commitment")
    if commitment["profile"] != EXCERPT_COMMITMENT_PROFILE:
        raise EvidenceSelectionValidationError("excerpt commitment profile mismatch")
    if not _sha256(commitment["sha256"]) or not _sha256(commitment["normalized_sha256"]):
        raise EvidenceSelectionValidationError("invalid excerpt commitment hash")
    for field in ("unicode_codepoints", "normalized_unicode_codepoints"):
        if isinstance(commitment[field], bool) or not isinstance(commitment[field], int) or commitment[field] < 0:
            raise EvidenceSelectionValidationError("invalid excerpt commitment length")
    _validate_hit_provenance(candidate["hit_provenance"], candidate["evidence_ids"])
    if candidate["candidate_id"] != _candidate_identity(candidate):
        raise EvidenceSelectionValidationError("candidate ledger identity mismatch")
    if candidate["priority"] != _priority(candidate):
        raise EvidenceSelectionValidationError("candidate ledger priority mismatch")
    duplicate = candidate["duplicate_equivalence"]
    _exact_keys(duplicate, {
        "profile", "key", "representative_candidate_id",
    }, "duplicate equivalence")
    if (
        duplicate["profile"] != DUPLICATE_EQUIVALENCE_PROFILE
        or duplicate["key"] != _duplicate_equivalence_key(candidate)
        or not _prefixed_sha256(duplicate["representative_candidate_id"], "cand_")
    ):
        raise EvidenceSelectionValidationError("invalid duplicate equivalence")
    cost = candidate["render_cost"]
    _exact_keys(cost, {"payload_chars", "estimated_tokens"}, "candidate render cost")
    if (
        isinstance(cost["payload_chars"], bool) or not isinstance(cost["payload_chars"], int)
        or cost["payload_chars"] < 0 or cost["estimated_tokens"] != math.ceil(cost["payload_chars"] / 4)
    ):
        raise EvidenceSelectionValidationError("invalid candidate render cost")
    return candidate


def _validate_selected_manifest(
    selected: list[Any], ledger: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    ledger_by_id = {item["candidate_id"]: item for item in ledger}
    result: dict[str, dict[str, Any]] = {}
    for item in selected:
        if not isinstance(item, dict):
            raise EvidenceSelectionValidationError("selected evidence must be an object")
        _exact_keys(item, {
            "candidate_id", "source", "excerpt", "evidence_ids", "hit_provenance",
            "duplicate_sources", "priority", "payload_chars", "estimated_tokens",
        }, "selected evidence")
        candidate_id = item["candidate_id"]
        if candidate_id in result or candidate_id not in ledger_by_id:
            raise EvidenceSelectionValidationError("selected evidence is not uniquely ledger-bound")
        if not isinstance(item["excerpt"], str):
            raise EvidenceSelectionValidationError("selected excerpt must be text")
        ledger_item = ledger_by_id[candidate_id]
        if _excerpt_commitment(item["excerpt"]) != ledger_item["excerpt_commitment"]:
            raise EvidenceSelectionValidationError("selected excerpt does not match the ledger commitment")
        for hit in ledger_item["hit_provenance"]:
            if hit["evidence_id"] != _v70_evidence_identity(ledger_item, hit, item["excerpt"]):
                raise EvidenceSelectionValidationError("selected excerpt does not match v7.0 evidence identity")
        expected = {
            "candidate_id": candidate_id,
            "source": deepcopy(ledger_item["source"]),
            "excerpt": item["excerpt"],
            "evidence_ids": deepcopy(ledger_item["evidence_ids"]),
            "hit_provenance": deepcopy(ledger_item["hit_provenance"]),
            "duplicate_sources": deepcopy(ledger_item["_derived_duplicate_sources"]),
            "priority": deepcopy(ledger_item["priority"]),
            "payload_chars": ledger_item["render_cost"]["payload_chars"],
            "estimated_tokens": ledger_item["render_cost"]["estimated_tokens"],
        }
        if item != expected:
            raise EvidenceSelectionValidationError("selected evidence does not match the canonical ledger")
        result[candidate_id] = item
    return result


def _v70_evidence_identity(
    candidate: dict[str, Any], hit: dict[str, Any], excerpt: str,
) -> str:
    evidence = {
        "classification": "unclassified_evidence",
        "result_rank": hit["result_rank"],
        "source": candidate["source"],
        "hit": {"type": hit["hit_type"], "score": hit["score"]},
        "explanation": {
            "basis_code": hit["basis_code"],
            "text": hit["explanation"],
        },
        "excerpt": excerpt,
        "details": hit["details"],
    }
    return "ev_" + _digest(evidence)


def _validate_source(source: Any, label: str) -> None:
    if not isinstance(source, dict):
        raise EvidenceSelectionValidationError(f"{label} must be an object")
    _exact_keys(source, {"conversation", "message_anchor", "reference_uri"}, label)
    conversation = source["conversation"]
    anchor = source["message_anchor"]
    _exact_keys(conversation, {"conversation_id", "title"}, f"{label} conversation")
    _exact_keys(anchor, {"message_id", "role", "time", "timestamp"}, f"{label} anchor")
    if (
        not all(isinstance(conversation[key], str) for key in conversation)
        or not conversation["conversation_id"]
        or not isinstance(anchor["message_id"], str) or not anchor["message_id"]
        or not all(isinstance(anchor[key], str) for key in ("role", "time"))
        or isinstance(anchor["timestamp"], bool) or not isinstance(anchor["timestamp"], (int, float))
        or not math.isfinite(float(anchor["timestamp"]))
    ):
        raise EvidenceSelectionValidationError(f"invalid {label}")
    expected_uri = f"ayunos://conversation/{conversation['conversation_id']}?message={anchor['message_id']}"
    if source["reference_uri"] != expected_uri:
        raise EvidenceSelectionValidationError(f"{label} reference mismatch")


def _validate_hit_provenance(value: Any, evidence_ids: Any) -> None:
    if not isinstance(value, list) or not value:
        raise EvidenceSelectionValidationError("candidate hit provenance is missing")
    if not isinstance(evidence_ids, list) or [hit.get("evidence_id") for hit in value] != evidence_ids:
        raise EvidenceSelectionValidationError("candidate evidence identities do not match provenance")
    if len(evidence_ids) != len(set(evidence_ids)):
        raise EvidenceSelectionValidationError("candidate evidence identities are not unique")
    prior_key = None
    for hit in value:
        if not isinstance(hit, dict):
            raise EvidenceSelectionValidationError("hit provenance must be an object")
        _exact_keys(hit, {
            "evidence_id", "result_rank", "hit_type", "score", "basis_code",
            "explanation", "details",
        }, "hit provenance")
        if (
            not _prefixed_sha256(hit["evidence_id"], "ev_")
            or isinstance(hit["result_rank"], bool) or not isinstance(hit["result_rank"], int)
            or hit["result_rank"] < 1 or hit["hit_type"] not in HIT_RANK
            or isinstance(hit["score"], bool) or not isinstance(hit["score"], (int, float))
            or not math.isfinite(float(hit["score"]))
            or hit["basis_code"] != hit["hit_type"] + "_retrieval_evidence"
            or not isinstance(hit["explanation"], str) or not hit["explanation"]
        ):
            raise EvidenceSelectionValidationError("invalid hit provenance")
        try:
            _validate_v70_evidence_details(hit["hit_type"], hit["details"])
        except MemoryPackageValidationError as exc:
            raise EvidenceSelectionValidationError("invalid v7.0 evidence details") from exc
        key = (HIT_RANK[hit["hit_type"]], hit["result_rank"], -float(hit["score"]), hit["evidence_id"])
        if prior_key is not None and key < prior_key:
            raise EvidenceSelectionValidationError("hit provenance is not canonically ordered")
        prior_key = key


def _expected_render_cost(
    ledger_item: dict[str, Any], duplicate_sources: list[dict[str, Any]],
) -> dict[str, int]:
    candidate = {
        "candidate_id": ledger_item["candidate_id"],
        "source": ledger_item["source"],
        "excerpt": "x" * ledger_item["excerpt_commitment"]["unicode_codepoints"],
        "hit_provenance": ledger_item["hit_provenance"],
        "duplicate_sources": duplicate_sources,
    }
    chars = len(_render_candidate(candidate))
    return {"payload_chars": chars, "estimated_tokens": math.ceil(chars / 4)}


def _validate_source_projection(
    projection: Any, ledger: list[dict[str, Any]],
) -> dict[str, Any]:
    if not isinstance(projection, dict):
        raise EvidenceSelectionValidationError("source_projection must be an object")
    _exact_keys(projection, {
        "protocol_version", "package_identity", "status", "reason_code", "readonly",
        "persisted", "request", "retrieval", "capabilities", "degradation", "warnings",
        "missing_capabilities", "evidence_order", "evidence_count",
    }, "source projection")
    source = deepcopy(projection)
    if (
        source["protocol_version"] != SOURCE_PROTOCOL_VERSION
        or not _prefixed_sha256(source["package_identity"], "mp_")
        or source["status"] not in PACKAGE_STATUSES
        or not _token(source["reason_code"])
        or source["readonly"] is not True or source["persisted"] is not False
    ):
        raise EvidenceSelectionValidationError("invalid v7.0 source projection binding")
    try:
        normalized_request = _validate_v70_request(source["request"])
        _validate_v70_retrieval(source["retrieval"])
        _validate_v70_capabilities(source["capabilities"], source["request"])
        _validate_v70_degradation(source["degradation"])
        _validate_v70_warnings(source["warnings"])
    except MemoryPackageValidationError as exc:
        raise EvidenceSelectionValidationError("invalid v7.0 source projection") from exc
    missing = source["missing_capabilities"]
    if (
        not isinstance(missing, list) or any(item not in HIT_RANK for item in missing)
        or missing != sorted(set(missing))
    ):
        raise EvidenceSelectionValidationError("invalid source missing_capabilities")
    evidence_order = source["evidence_order"]
    if (
        not isinstance(evidence_order, list)
        or any(not _prefixed_sha256(item, "ev_") for item in evidence_order)
        or len(evidence_order) != len(set(evidence_order))
        or isinstance(source["evidence_count"], bool)
        or source["evidence_count"] != len(evidence_order)
    ):
        raise EvidenceSelectionValidationError("invalid source evidence order")
    hits = [hit for candidate in ledger for hit in candidate["hit_provenance"]]
    expected_order = [
        hit["evidence_id"] for hit in sorted(
            hits, key=lambda hit: (hit["result_rank"], HIT_RANK[hit["hit_type"]], hit["evidence_id"]),
        )
    ]
    if evidence_order != expected_order:
        raise EvidenceSelectionValidationError("source evidence order does not match the ledger")

    states = []
    for candidate in ledger:
        for hit in candidate["hit_provenance"]:
            try:
                states.append(project_retrieval_evidence_state(
                    hit["result_rank"], hit["hit_type"], hit["details"],
                    conversation_id=_conversation_id(candidate),
                    anchor_message_id=_message_id(candidate),
                ))
            except RetrievalValidationError as exc:
                raise EvidenceSelectionValidationError("source projection graph provenance mismatch") from exc
    retrieval = source["retrieval"]
    try:
        validate_retrieval_envelope_state(
            normalized_request,
            status=retrieval["status"], reason_code=retrieval["reason_code"],
            availability=retrieval["availability"], result_count=retrieval["result_count"],
            total_ranked=retrieval["total_ranked"], next_cursor=retrieval["next_cursor"],
            result_ranks=[item.rank for item in states], evidence=states,
        )
    except RetrievalValidationError as exc:
        raise EvidenceSelectionValidationError("source retrieval state is not reproducible") from exc
    capabilities = expected_retrieval_capabilities(normalized_request, retrieval["availability"])
    expected_missing = sorted(name for name, state in capabilities.items() if state["status"] == "unavailable")
    expected_degradation = {
        "status": "unavailable" if source["status"] == "unavailable" else "degraded" if expected_missing else "complete",
        "reason_codes": sorted(capabilities[name]["reason_code"] for name in expected_missing),
    }
    expected_warnings = [{
        "code": f"{name}_unavailable",
        "message": f"{name} capability unavailable: {capabilities[name]['reason_code']}",
    } for name in expected_missing]
    if (
        source["capabilities"] != capabilities
        or source["missing_capabilities"] != expected_missing
        or source["degradation"] != expected_degradation
        or source["warnings"] != expected_warnings
    ):
        raise EvidenceSelectionValidationError("source capability projection mismatch")
    for hit in hits:
        if (
            hit["hit_type"] not in source["request"]["scope"]["hit_types"]
            or capabilities[hit["hit_type"]]["status"] != "available"
        ):
            raise EvidenceSelectionValidationError("ledger evidence is not source-capability bound")
    source_unavailable = retrieval["availability"]["source_database"]["status"] == "unavailable"
    expected_status = (
        "unavailable" if source_unavailable else "empty" if not ledger
        else "partial" if expected_missing else "ok"
    )
    expected_reason = (
        retrieval["reason_code"] if expected_status == "unavailable" else
        "no_evidence" if expected_status == "empty" else
        "partial_capability_result" if expected_status == "partial" else "package_complete"
    )
    if (source["status"], source["reason_code"]) != (expected_status, expected_reason):
        raise EvidenceSelectionValidationError("source package status projection mismatch")
    return source


def _validate_omitted(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise EvidenceSelectionValidationError("omitted evidence must be an object")
    _exact_keys(item, {
        "candidate_id", "evidence_ids", "source_anchor", "reason", "duplicate_of",
        "priority", "payload_chars", "estimated_tokens", "hit_provenance",
    }, "omitted evidence")
    if item["reason"] not in OMISSION_REASONS:
        raise EvidenceSelectionValidationError("invalid omission reason")
    if item["reason"] == "duplicate" and not item["duplicate_of"]:
        raise EvidenceSelectionValidationError("duplicate omission requires duplicate_of")
    if item["reason"] != "duplicate" and item["duplicate_of"] is not None:
        raise EvidenceSelectionValidationError("non-duplicate omission cannot have duplicate_of")
    if not isinstance(item["payload_chars"], int) or item["payload_chars"] < 0:
        raise EvidenceSelectionValidationError("invalid omitted payload cost")
    if item["estimated_tokens"] != math.ceil(item["payload_chars"] / 4):
        raise EvidenceSelectionValidationError("invalid omitted token estimate")
    if [hit.get("evidence_id") for hit in item["hit_provenance"]] != item["evidence_ids"]:
        raise EvidenceSelectionValidationError("omitted hit provenance mismatch")
    _exact_keys(item["source_anchor"], {
        "conversation_id", "message_id", "reference_uri", "role", "time", "timestamp",
    }, "omitted source anchor")
    if item["source_anchor"]["reference_uri"] != (
        f"ayunos://conversation/{item['source_anchor']['conversation_id']}"
        f"?message={item['source_anchor']['message_id']}"
    ):
        raise EvidenceSelectionValidationError("omitted source reference mismatch")
    for hit in item["hit_provenance"]:
        _exact_keys(hit, {
            "evidence_id", "result_rank", "hit_type", "score", "basis_code",
            "explanation", "details",
        }, "omitted hit provenance")
        if hit["hit_type"] not in HIT_RANK:
            raise EvidenceSelectionValidationError("invalid omitted hit type")
    if "excerpt" in item or "explanation" in item or "details" in item:
        raise EvidenceSelectionValidationError("omitted evidence must not contain body text")
    return item


def _validate_counts(
    package: dict[str, Any], ledger: list[dict[str, Any]], selected: list[dict[str, Any]],
    omitted: list[dict[str, Any]], source: dict[str, Any],
) -> None:
    counts = package["counts"]
    _exact_keys(counts, {
        "input_evidence", "merged_anchors", "deduplicated_candidates", "selected",
        "omitted", "omission_reasons", "selected_conversations",
    }, "counts")
    reason_counts = {reason: 0 for reason in OMISSION_REASONS}
    for item in omitted:
        reason_counts[item["reason"]] += 1
    duplicate_count = reason_counts["duplicate"]
    expected = {
        "input_evidence": source["evidence_count"],
        "merged_anchors": len(ledger),
        "deduplicated_candidates": len(ledger) - duplicate_count,
        "selected": len(selected),
        "omitted": len(omitted),
        "omission_reasons": reason_counts,
        "selected_conversations": len({_conversation_id(item) for item in selected}),
    }
    if counts != expected:
        raise EvidenceSelectionValidationError("selection counts mismatch")


def _validate_budget(package: dict[str, Any], policy: dict[str, Any], payload: str) -> None:
    budget = package["budget"]
    _exact_keys(budget, {
        "character_budget", "used_characters", "remaining_characters",
        "character_hard_limit_satisfied", "estimated_token_budget", "estimated_tokens_used",
        "estimated_tokens_remaining", "estimator_profile", "exact_model_tokenizer",
    }, "budget")
    used = len(payload)
    estimated = estimate_tokens(payload)
    expected = {
        "character_budget": policy["character_budget"],
        "used_characters": used,
        "remaining_characters": policy["character_budget"] - used,
        "character_hard_limit_satisfied": used <= policy["character_budget"],
        "estimated_token_budget": policy["estimated_token_budget"],
        "estimated_tokens_used": estimated,
        "estimated_tokens_remaining": policy["estimated_token_budget"] - estimated,
        "estimator_profile": ESTIMATOR_PROFILE,
        "exact_model_tokenizer": False,
    }
    if budget != expected or not expected["character_hard_limit_satisfied"] or estimated > policy["estimated_token_budget"]:
        raise EvidenceSelectionValidationError("selection budget mismatch")


def _selection_status(
    source_status: str, source_reason: str, selected: bool, any_candidate: bool,
) -> tuple[str, str]:
    if source_status == "unavailable":
        return "unavailable", source_reason
    if not any_candidate:
        return "empty", "no_evidence"
    if not selected:
        return "empty", "budget_excluded_all"
    if source_status == "partial":
        return "partial", "partial_capability_selection"
    return "ok", "selection_complete"


def _conversation_id(candidate: dict[str, Any]) -> str:
    if "source" in candidate:
        return candidate["source"]["conversation"]["conversation_id"]
    return candidate["source_anchor"]["conversation_id"]


def _message_id(candidate: dict[str, Any]) -> str:
    if "source" in candidate:
        return candidate["source"]["message_anchor"]["message_id"]
    return candidate["source_anchor"]["message_id"]


def _identity_payload(package: dict[str, Any]) -> dict[str, Any]:
    return {key: deepcopy(value) for key, value in package.items() if key != "package_identity"}


def _canonical_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_dump(value).encode("utf-8")).hexdigest()


def _exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise EvidenceSelectionValidationError(f"{label} keys mismatch")


def _token(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and all(char.isalnum() or char in "._-" for char in value)


def _sha256(value: Any) -> bool:
    return (
        isinstance(value, str) and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _prefixed_sha256(value: Any, prefix: str) -> bool:
    return isinstance(value, str) and value.startswith(prefix) and _sha256(value[len(prefix):])
