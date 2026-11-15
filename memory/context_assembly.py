"""YunOS v7.3 deterministic, readonly context assembly.

The layer consumes the sealed v7.0, v7.1, and v7.2 packages.  It performs
task relevance filtering, priority ordering, hard budgeting, and transparent
assembly without calling a model or persisting any state.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Iterable
import unicodedata

from memory.db_path_policy import DEFAULT_REAL_DB_PATH
from memory.evidence_selection import (
    ESTIMATOR_PROFILE,
    PROTOCOL_VERSION as EVIDENCE_SELECTION_PROTOCOL_VERSION,
    EvidenceSelectionValidationError,
    build_selection_from_memory_package,
    normalize_policy as normalize_v71_policy,
    validate_evidence_selection_source_backed,
    validate_evidence_selection_standalone,
)
from memory.graph_enhanced_retrieval import (
    PROTOCOL_VERSION as RETRIEVAL_PROTOCOL_VERSION,
    RetrievalQuery,
)
from memory.memory_package import (
    PROTOCOL_VERSION as MEMORY_PACKAGE_PROTOCOL_VERSION,
    MemoryPackageService,
    MemoryPackageValidationError,
    _redacted_text as _v70_redacted_text,
    _reject_absolute_paths as _v70_reject_absolute_paths,
    validate_memory_package_source_backed,
    validate_memory_package_standalone,
)
from memory.runtime_protocol_contracts import profile_item_identity
from memory.temporal_memory_candidates import (
    PROTOCOL_VERSION as TEMPORAL_CANDIDATE_PROTOCOL_VERSION,
    TemporalCandidateValidationError,
    build_candidates_from_selection,
    default_timezone_config,
    validate_temporal_candidates_source_backed,
    validate_temporal_candidates_standalone,
)


PROTOCOL_VERSION = "v7.3-context-assembly-2"
SELECTION_POLICY_VERSION = "v7.3-context-selection-policy-2"
ASSEMBLY_IDENTITY_VERSION = "v7.3-context-assembly-identity-2"
RELEVANCE_PROFILE = "latin-exact-token-han-conservative-v2"
APPROVED_PROFILE_SCHEMA = "v7.3-approved-profile-input-1"
SOURCE_SELECTION_PROFILE = "Large"

PROFILE_KINDS = ("fact", "preference", "goal", "decision", "constraint")
OMISSION_REASONS = (
    "irrelevant_to_task",
    "candidate_count_limit",
    "evidence_count_limit",
    "history_expansion_limit",
    "character_budget",
    "estimated_token_budget",
    "relation_endpoint_omitted",
    "source_selection_budget_exceeded",
    "source_selection_conversation_cap",
    "source_selection_duplicate",
    "source_selection_lower_priority",
)


class ContextAssemblyValidationError(ValueError):
    """The v7.3 policy or assembly is invalid or cannot be reproduced."""


@dataclass(frozen=True)
class BudgetProfile:
    name: str
    total_characters: int
    estimated_tokens: int
    max_evidence: int
    max_candidates: int
    max_history_expansions: int


BUILTIN_PROFILES = {
    "small": BudgetProfile("Small", 2_000, 500, 4, 2, 0),
    "medium": BudgetProfile("Medium", 6_000, 1_500, 12, 6, 0),
    "large": BudgetProfile("Large", 16_000, 4_000, 30, 16, 2),
}
CUSTOM_LIMITS = {
    "total_characters": (512, 50_000),
    "estimated_tokens": (128, 12_500),
    "max_evidence": (0, 100),
    "max_candidates": (0, 100),
    "max_history_expansions": (0, 20),
}

_STOP_TERMS = frozenset({
    "about", "also", "and", "are", "buy", "current", "for", "from", "help",
    "history", "how", "into", "looking", "memory", "need", "please", "purchase",
    "should", "task", "that", "the", "this", "use", "want", "what", "when", "which",
    "with", "would", "you", "your", "一下", "什么", "关于", "当前", "帮我", "想买",
    "我们", "我的", "是否", "购买", "请问", "这个", "需要", "问题",
})
_LATIN_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[._+-][a-z0-9]+)*")
_HAN_SEGMENT_RE = re.compile(r"[\u3400-\u9fff]+")


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_dump(value).encode("utf-8")).hexdigest()


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / 4)


def redact_context_text(text: str) -> str:
    """Apply the sealed v7.0 absolute-path clipping/redaction boundary."""
    return _v70_redacted_text(" " + str(text))[1:]


def _reject_context_absolute_paths(value: Any) -> None:
    """Use v7.0's detector while making string-start POSIX paths observable."""
    if isinstance(value, str):
        _v70_reject_absolute_paths(" " + value)
        return
    if isinstance(value, list):
        for item in value:
            _reject_context_absolute_paths(item)
        return
    if isinstance(value, dict):
        for item in value.values():
            _reject_context_absolute_paths(item)


def _project_warnings(warnings: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    return [{
        "code": redact_context_text(item["code"]),
        "message": redact_context_text(item["message"]),
    } for item in warnings]


def normalize_assembly_policy(
    budget_profile: str,
    *,
    total_characters: int | None = None,
    estimated_tokens: int | None = None,
    max_evidence: int | None = None,
    max_candidates: int | None = None,
    max_history_expansions: int | None = None,
) -> dict[str, Any]:
    if not isinstance(budget_profile, str):
        raise ContextAssemblyValidationError("budget_profile must be Small, Medium, Large, or Custom")
    key = budget_profile.strip().casefold()
    overrides = {
        "total_characters": total_characters,
        "estimated_tokens": estimated_tokens,
        "max_evidence": max_evidence,
        "max_candidates": max_candidates,
        "max_history_expansions": max_history_expansions,
    }
    if key in BUILTIN_PROFILES:
        if any(value is not None for value in overrides.values()):
            raise ContextAssemblyValidationError("built-in assembly profiles do not accept overrides")
        profile = BUILTIN_PROFILES[key]
    elif key == "custom":
        for name, value in overrides.items():
            low, high = CUSTOM_LIMITS[name]
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ContextAssemblyValidationError(
                    f"Custom {name} must be an integer between {low} and {high}"
                )
        profile = BudgetProfile("Custom", **overrides)
    else:
        raise ContextAssemblyValidationError("budget_profile must be Small, Medium, Large, or Custom")
    policy = {
        "policy_version": SELECTION_POLICY_VERSION,
        "policy_identity": "",
        "budget_profile": profile.name,
        "limits": {
            "total_characters": profile.total_characters,
            "estimated_tokens": profile.estimated_tokens,
            "max_evidence": profile.max_evidence,
            "max_candidates": profile.max_candidates,
            "max_history_expansions": profile.max_history_expansions,
        },
        "priority_order": [
            "current_task", "confirmed_user_profile", "candidate_memory", "historical_evidence",
        ],
        "relevance_profile": RELEVANCE_PROFILE,
        "history_default": "citation_and_anchor_only",
        "semantic_truncation": False,
        "token_estimator": {
            "profile": ESTIMATOR_PROFILE,
            "method": "ceil(unicode_code_points/4)",
            "exact_model_tokenizer": False,
        },
        "source_selection": {
            "protocol": "v7.1",
            "budget_profile": SOURCE_SELECTION_PROFILE,
        },
    }
    policy["policy_identity"] = "csp_" + _digest({k: v for k, v in policy.items() if k != "policy_identity"})
    return _validate_policy(policy)


def make_approved_profile_item(
    statement: str,
    *,
    kind: str,
    topics: Iterable[str],
    approval_id: str,
    approved_at: str,
) -> dict[str, Any]:
    item = {
        "schema_version": APPROVED_PROFILE_SCHEMA,
        "profile_item_id": "",
        "kind": kind,
        "statement": redact_context_text(statement),
        "topics": sorted(set(_topic_terms(redact_context_text(" ".join(topics)), allow_fallback=True))),
        "approval": {
            "status": "approved",
            "authority": "user",
            "approval_id": approval_id,
            "approved_at": approved_at,
        },
    }
    item["profile_item_id"] = profile_item_identity(item)
    return _validate_approved_profile_item(item)


class _ContextAssemblyCore:
    """Private injection seam for synthetic tests; production has no such seam."""

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
        total_characters: int | None = None,
        estimated_tokens: int | None = None,
        max_evidence: int | None = None,
        max_candidates: int | None = None,
        max_history_expansions: int | None = None,
        approved_profile: Iterable[dict[str, Any]] = (),
    ) -> dict[str, Any]:
        policy = normalize_assembly_policy(
            budget_profile,
            total_characters=total_characters,
            estimated_tokens=estimated_tokens,
            max_evidence=max_evidence,
            max_candidates=max_candidates,
            max_history_expansions=max_history_expansions,
        )
        memory_package = self.memory_package_service.build(query)
        selection = build_selection_from_memory_package(
            memory_package, policy=normalize_v71_policy(SOURCE_SELECTION_PROFILE),
        )
        validate_evidence_selection_source_backed(
            selection,
            trusted_source_package=memory_package,
            trusted_source_db=self.source_db,
        )
        temporal = build_candidates_from_selection(
            selection, timezone_config=default_timezone_config(),
        )
        validate_temporal_candidates_source_backed(
            temporal,
            trusted_selection=selection,
            trusted_source_package=memory_package,
            trusted_source_db=self.source_db,
        )
        approved = tuple(_validate_approved_profile_item(deepcopy(item)) for item in approved_profile)
        package = build_context_assembly_from_packages(
            memory_package,
            selection,
            temporal,
            policy=policy,
            approved_profile=approved,
        )
        validate_context_assembly_source_backed(
            package,
            trusted_memory_package=memory_package,
            trusted_selection=selection,
            trusted_temporal_candidates=temporal,
            trusted_source_db=self.source_db,
            trusted_approved_profile=approved,
        )
        return package


class ContextAssemblyService:
    """Fixed-production service: no path, provider, or profile injection."""

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
    ) -> dict[str, Any]:
        return _ContextAssemblyCore().build(
            query,
            budget_profile=budget_profile,
            total_characters=total_characters,
            estimated_tokens=estimated_tokens,
            max_evidence=max_evidence,
            max_candidates=max_candidates,
            max_history_expansions=max_history_expansions,
            approved_profile=(),
        )


def build_production_context_assembly(
    query: RetrievalQuery,
    *,
    budget_profile: str = "Medium",
    total_characters: int | None = None,
    estimated_tokens: int | None = None,
    max_evidence: int | None = None,
    max_candidates: int | None = None,
    max_history_expansions: int | None = None,
) -> dict[str, Any]:
    return ContextAssemblyService().build(
        query,
        budget_profile=budget_profile,
        total_characters=total_characters,
        estimated_tokens=estimated_tokens,
        max_evidence=max_evidence,
        max_candidates=max_candidates,
        max_history_expansions=max_history_expansions,
    )


def build_context_assembly_from_packages(
    memory_package: dict[str, Any],
    selection: dict[str, Any],
    temporal_candidates: dict[str, Any],
    *,
    policy: dict[str, Any],
    approved_profile: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    validate_memory_package_standalone(memory_package)
    validate_evidence_selection_standalone(selection)
    validate_temporal_candidates_standalone(temporal_candidates)
    policy = deepcopy(_validate_policy(policy))
    if selection["source_projection"]["package_identity"] != memory_package["package_identity"]:
        raise ContextAssemblyValidationError("v7.1 does not bind the supplied v7.0 package")
    if temporal_candidates["source_projection"]["package_identity"] != selection["package_identity"]:
        raise ContextAssemblyValidationError("v7.2 does not bind the supplied v7.1 package")
    approved = [_validate_approved_profile_item(deepcopy(item)) for item in approved_profile]
    if len({item["profile_item_id"] for item in approved}) != len(approved):
        raise ContextAssemblyValidationError("duplicate approved profile identity")

    request = deepcopy(selection["request"])
    current_question = redact_context_text(request["task_or_query"])
    topics = _topic_terms(current_question, allow_fallback=True)
    selected_by_id = {item["candidate_id"]: item for item in selection["selected"]}
    events_by_id = {item["event_id"]: item for item in temporal_candidates["events"]}
    omitted: list[dict[str, Any]] = []

    approved_ranked = []
    for item in approved:
        matched = _matched_topics(topics, item["statement"] + " " + " ".join(item["topics"]))
        if not matched:
            omitted.append(_omission("confirmed_user_profile", "approved_profile", item["profile_item_id"], "irrelevant_to_task", 2))
            continue
        approved_ranked.append((_relevance_sort_key(matched, item["profile_item_id"]), _project_profile(item, matched)))
    approved_ranked.sort(key=lambda pair: pair[0])

    candidate_ranked = []
    for item in temporal_candidates["candidates"]:
        event = events_by_id[item["event_id"]]
        selected = selected_by_id[event["source_candidate_id"]]
        corpus = " ".join((item["statement"], selected["excerpt"], selected["source"]["conversation"]["title"]))
        matched = _matched_topics(topics, corpus)
        if not matched:
            omitted.append(_omission("candidate_memory", "candidate", item["candidate_id"], "irrelevant_to_task", 3))
            continue
        projected = _project_candidate(item, event, matched)
        candidate_ranked.append((_candidate_sort_key(projected), projected))
    candidate_ranked.sort(key=lambda pair: pair[0])

    evidence_ranked = []
    for index, item in enumerate(selection["selected"]):
        corpus = " ".join((item["excerpt"], item["source"]["conversation"]["title"]))
        matched = _matched_topics(topics, corpus)
        if not matched:
            omitted.append(_omission("historical_evidence", "evidence", item["candidate_id"], "irrelevant_to_task", 4))
            continue
        evidence_ranked.append((index, _project_evidence(item, matched), item["excerpt"]))

    for item in selection["omitted"]:
        omitted.append(_omission(
            "historical_evidence", "source_evidence", item["candidate_id"],
            f"source_selection_{item['reason']}", 4,
        ))

    source_chain = _source_chain(memory_package, selection, temporal_candidates)
    task_context = {
        "current_user_question": current_question,
        "query_topics": topics,
        "time_range": deepcopy(request["time"]),
        "current_retrieval_results": {
            "status": selection["status"],
            "reason_code": selection["reason_code"],
            "capabilities": deepcopy(selection["capabilities"]),
            "degradation": deepcopy(selection["degradation"]),
            "missing_capabilities": deepcopy(selection["missing_capabilities"]),
            "warnings": _project_warnings(selection["warnings"]),
            "source_selected_count": len(selection["selected"]),
            "task_relevant_count": len(evidence_ranked),
            "assembled_reference_ids": [],
        },
    }
    confirmed_layer = {
        "authority": "independently_supplied_approved_profile",
        "production_input": "none",
        "candidate_promotion_allowed": False,
        "items": [],
    }
    candidate_layer = {
        "candidate_only": True,
        "may_be_used_as_auxiliary_reference": True,
        "items": [],
        "relations": [],
    }
    evidence_layer = {
        "default_representation": "citation_and_anchor_only",
        "items": [],
        "expanded_count": 0,
    }

    limits = policy["limits"]

    def fits() -> tuple[bool, str | None]:
        text = _render_model_context(task_context, confirmed_layer, candidate_layer, evidence_layer)
        if len(text) > limits["total_characters"]:
            return False, "character_budget"
        if estimate_tokens(text) > limits["estimated_tokens"]:
            return False, "estimated_token_budget"
        return True, None

    ok, reason = fits()
    if not ok:
        raise ContextAssemblyValidationError(f"current task exceeds the {reason}")

    for _, item in approved_ranked:
        confirmed_layer["items"].append(item)
        ok, reason = fits()
        if not ok:
            confirmed_layer["items"].pop()
            omitted.append(_omission("confirmed_user_profile", "approved_profile", item["profile_item_id"], reason, 2))

    for index, (_, item) in enumerate(candidate_ranked):
        if index >= limits["max_candidates"]:
            omitted.append(_omission("candidate_memory", "candidate", item["candidate_id"], "candidate_count_limit", 3))
            continue
        candidate_layer["items"].append(item)
        ok, reason = fits()
        if not ok:
            candidate_layer["items"].pop()
            omitted.append(_omission("candidate_memory", "candidate", item["candidate_id"], reason, 3))

    included_candidate_ids = {item["candidate_id"] for item in candidate_layer["items"]}
    for relation in temporal_candidates["temporal_relations"]:
        if not {relation["newer_candidate_id"], relation["older_candidate_id"]} <= included_candidate_ids:
            omitted.append(_omission("candidate_memory", "temporal_relation", relation["relation_id"], "relation_endpoint_omitted", 3))
            continue
        projected = deepcopy(relation)
        candidate_layer["relations"].append(projected)
        ok, reason = fits()
        if not ok:
            candidate_layer["relations"].pop()
            omitted.append(_omission("candidate_memory", "temporal_relation", relation["relation_id"], reason, 3))

    included_excerpt_by_id: dict[str, str] = {}
    for index, projected, excerpt in evidence_ranked:
        if len(evidence_layer["items"]) >= limits["max_evidence"]:
            omitted.append(_omission("historical_evidence", "evidence", projected["evidence_reference_id"], "evidence_count_limit", 4))
            continue
        evidence_layer["items"].append(projected)
        task_context["current_retrieval_results"]["assembled_reference_ids"].append(projected["evidence_reference_id"])
        ok, reason = fits()
        if not ok:
            evidence_layer["items"].pop()
            task_context["current_retrieval_results"]["assembled_reference_ids"].pop()
            omitted.append(_omission("historical_evidence", "evidence", projected["evidence_reference_id"], reason, 4))
            continue
        included_excerpt_by_id[projected["evidence_reference_id"]] = excerpt

    expansion_slots = limits["max_history_expansions"]
    for item in evidence_layer["items"]:
        item_id = item["evidence_reference_id"]
        if evidence_layer["expanded_count"] >= expansion_slots:
            if expansion_slots > 0:
                omitted.append(_omission("historical_evidence", "history_expansion", item_id, "history_expansion_limit", 4))
            continue
        item["expanded_excerpt"] = included_excerpt_by_id[item_id]
        evidence_layer["expanded_count"] += 1
        ok, reason = fits()
        if not ok:
            item["expanded_excerpt"] = None
            evidence_layer["expanded_count"] -= 1
            omitted.append(_omission("historical_evidence", "history_expansion", item_id, reason, 4))

    model_context = _render_model_context(task_context, confirmed_layer, candidate_layer, evidence_layer)
    omitted.sort(key=lambda item: (item["priority"], item["layer"], item["item_type"], item["item_id"], item["reason"]))
    status, reason_code = _assembly_status(selection, temporal_candidates, confirmed_layer, candidate_layer, evidence_layer)
    usage = {
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
    package = {
        "protocol_version": PROTOCOL_VERSION,
        "assembly_identity_version": ASSEMBLY_IDENTITY_VERSION,
        "assembly_identity": "",
        "selection_policy_identity": policy["policy_identity"],
        "status": status,
        "reason_code": reason_code,
        "readonly": True,
        "persisted": False,
        "external_model_calls": False,
        "source_chain": source_chain,
        "selection_policy": policy,
        "task_context": task_context,
        "confirmed_user_profile": confirmed_layer,
        "candidate_memory": candidate_layer,
        "historical_evidence": evidence_layer,
        "omitted": omitted,
        "counts": {
            "approved_profile_used": len(confirmed_layer["items"]),
            "candidates_used": len(candidate_layer["items"]),
            "candidate_relations_used": len(candidate_layer["relations"]),
            "evidence_used": len(evidence_layer["items"]),
            "history_expanded": evidence_layer["expanded_count"],
            "omitted": len(omitted),
            "omission_reasons": {name: sum(item["reason"] == name for item in omitted) for name in OMISSION_REASONS},
        },
        "budget": usage,
        "transparency": {
            "description": "本次回答模型看到了什么",
            "model_saw": {
                "approved_profile_ids": [item["profile_item_id"] for item in confirmed_layer["items"]],
                "candidate_ids": [item["candidate_id"] for item in candidate_layer["items"]],
                "evidence_reference_ids": [item["evidence_reference_id"] for item in evidence_layer["items"]],
                "expanded_evidence_ids": [item["evidence_reference_id"] for item in evidence_layer["items"] if item["expanded_excerpt"] is not None],
            },
            "excluded": deepcopy(omitted),
            "usage": deepcopy(usage),
        },
        "model_context": model_context,
    }
    package["assembly_identity"] = "ctx_" + _digest(_identity_payload(package))
    validate_context_assembly_standalone(package)
    return package


def canonical_context_assembly_json(package: dict[str, Any]) -> str:
    validate_context_assembly_standalone(package)
    return _dump(package)


def render_context_assembly(package: dict[str, Any]) -> str:
    validate_context_assembly_standalone(package)
    budget = package["budget"]
    lines = [
        f"YunOS Context Assembly {package['protocol_version']}",
        f"assembly_identity: {package['assembly_identity']}",
        f"selection_policy_identity: {package['selection_policy_identity']}",
        f"status: {package['status']} ({package['reason_code']})",
        f"characters: {budget['used_characters']}/{budget['total_characters_limit']} (hard_limit=true)",
        f"estimated_tokens: {budget['estimated_tokens_used']}/{budget['estimated_token_limit']} ({budget['estimator_profile']}; not_model_exact)",
        "model_saw: approved=" + ",".join(package["transparency"]["model_saw"]["approved_profile_ids"] or ["none"]),
        "model_saw: candidates=" + ",".join(package["transparency"]["model_saw"]["candidate_ids"] or ["none"]),
        "model_saw: evidence=" + ",".join(package["transparency"]["model_saw"]["evidence_reference_ids"] or ["none"]),
        "excluded:",
    ]
    if package["omitted"]:
        lines.extend(
            f"- {item['layer']}/{item['item_type']}/{item['item_id']}: {item['reason']}"
            for item in package["omitted"]
        )
    else:
        lines.append("- none")
    lines.extend(("MODEL_CONTEXT_BEGIN", package["model_context"], "MODEL_CONTEXT_END"))
    return "\n".join(lines) + "\n"


def validate_context_assembly_standalone(package: dict[str, Any]) -> None:
    if not isinstance(package, dict):
        raise ContextAssemblyValidationError("context assembly must be an object")
    _exact_keys(package, {
        "protocol_version", "assembly_identity_version", "assembly_identity",
        "selection_policy_identity", "status", "reason_code", "readonly", "persisted",
        "external_model_calls", "source_chain", "selection_policy", "task_context",
        "confirmed_user_profile", "candidate_memory", "historical_evidence", "omitted",
        "counts", "budget", "transparency", "model_context",
    }, "context assembly")
    if package["protocol_version"] != PROTOCOL_VERSION:
        raise ContextAssemblyValidationError("context assembly protocol mismatch")
    if package["assembly_identity_version"] != ASSEMBLY_IDENTITY_VERSION:
        raise ContextAssemblyValidationError("assembly identity version mismatch")
    if package["status"] not in {"ok", "partial", "unavailable"} or not isinstance(
        package["reason_code"], str
    ) or not re.fullmatch(r"[a-z][a-z0-9_]{0,99}", package["reason_code"]):
        raise ContextAssemblyValidationError("invalid assembly status or reason")
    if package["readonly"] is not True or package["persisted"] is not False or package["external_model_calls"] is not False:
        raise ContextAssemblyValidationError("v7.3 must remain readonly, non-persisted, and model-free")
    policy = _validate_policy(package["selection_policy"])
    if package["selection_policy_identity"] != policy["policy_identity"]:
        raise ContextAssemblyValidationError("selection policy identity mismatch")
    _validate_source_chain(package["source_chain"])
    _validate_task_context(package["task_context"])
    _validate_layers(package)
    source = package["source_chain"]
    current = package["task_context"]["current_retrieval_results"]
    if (current["status"], current["reason_code"]) != (
        source["evidence_selection"]["status"], source["evidence_selection"]["reason_code"],
    ):
        raise ContextAssemblyValidationError("current retrieval state does not match v7.1")
    for field in ("capabilities", "degradation", "missing_capabilities", "warnings"):
        if current[field] != source["evidence_selection"][field]:
            raise ContextAssemblyValidationError(f"current retrieval {field} does not match v7.1")
    expected_status = _assembly_status(
        source["evidence_selection"], source["temporal_candidates"],
        package["confirmed_user_profile"], package["candidate_memory"],
        package["historical_evidence"],
    )
    if (package["status"], package["reason_code"]) != expected_status:
        raise ContextAssemblyValidationError("assembly status derivation mismatch")
    expected_context = _render_model_context(
        package["task_context"], package["confirmed_user_profile"],
        package["candidate_memory"], package["historical_evidence"],
    )
    if package["model_context"] != expected_context:
        raise ContextAssemblyValidationError("model context does not match the structured layers")
    _validate_budget(package, policy)
    _validate_omissions_counts_and_transparency(package)
    if package["assembly_identity"] != "ctx_" + _digest(_identity_payload(package)):
        raise ContextAssemblyValidationError("assembly identity mismatch")
    try:
        _reject_context_absolute_paths(package)
    except MemoryPackageValidationError as exc:
        raise ContextAssemblyValidationError(
            "context assembly violates the v7.0 absolute-path boundary"
        ) from exc


def validate_context_assembly_source_backed(
    package: dict[str, Any],
    *,
    trusted_memory_package: dict[str, Any],
    trusted_selection: dict[str, Any],
    trusted_temporal_candidates: dict[str, Any],
    trusted_source_db: str | Path,
    trusted_approved_profile: Iterable[dict[str, Any]] = (),
) -> None:
    """Authenticate upstream SQLite evidence and reproduce the exact assembly."""
    validate_context_assembly_standalone(package)
    validate_memory_package_source_backed(trusted_memory_package, trusted_source_db=trusted_source_db)
    validate_evidence_selection_source_backed(
        trusted_selection,
        trusted_source_package=trusted_memory_package,
        trusted_source_db=trusted_source_db,
    )
    validate_temporal_candidates_source_backed(
        trusted_temporal_candidates,
        trusted_selection=trusted_selection,
        trusted_source_package=trusted_memory_package,
        trusted_source_db=trusted_source_db,
    )
    rebuilt = build_context_assembly_from_packages(
        trusted_memory_package,
        trusted_selection,
        trusted_temporal_candidates,
        policy=package["selection_policy"],
        approved_profile=trusted_approved_profile,
    )
    if package != rebuilt:
        raise ContextAssemblyValidationError("source-backed context assembly reconstruction mismatch")


def _source_chain(memory_package: dict[str, Any], selection: dict[str, Any], temporal: dict[str, Any]) -> dict[str, Any]:
    return {
        "retrieval": {
            "protocol_version": memory_package["retrieval"]["protocol_version"],
            "status": memory_package["retrieval"]["status"],
            "reason_code": memory_package["retrieval"]["reason_code"],
        },
        "memory_package": {
            "protocol_version": memory_package["protocol_version"],
            "package_identity": memory_package["package_identity"],
            "status": memory_package["status"],
            "reason_code": memory_package["reason_code"],
        },
        "evidence_selection": {
            "protocol_version": selection["protocol_version"],
            "package_identity": selection["package_identity"],
            "status": selection["status"],
            "reason_code": selection["reason_code"],
            "capabilities": deepcopy(selection["capabilities"]),
            "degradation": deepcopy(selection["degradation"]),
            "warnings": _project_warnings(selection["warnings"]),
            "missing_capabilities": deepcopy(selection["missing_capabilities"]),
        },
        "temporal_candidates": {
            "protocol_version": temporal["protocol_version"],
            "package_identity": temporal["package_identity"],
            "status": temporal["status"],
            "reason_code": temporal["reason_code"],
            "candidate_only": True,
        },
    }


def _project_profile(item: dict[str, Any], matched: list[str]) -> dict[str, Any]:
    return {**deepcopy(item), "relevance": {"profile": RELEVANCE_PROFILE, "matched_topics": matched}}


def _project_candidate(item: dict[str, Any], event: dict[str, Any], matched: list[str]) -> dict[str, Any]:
    return {
        "candidate_id": item["candidate_id"],
        "state": "candidate",
        "approval_status": "not_approved",
        "candidate_type": item["candidate_type"],
        "statement": item["statement"],
        "review_status": item["review_status"],
        "confidence": item["confidence"],
        "observed_at_utc": item["evidence"]["observed_at_utc"],
        "source_candidate_id": event["source_candidate_id"],
        "reference_uri": item["evidence"]["reference_uri"],
        "relevance": {"profile": RELEVANCE_PROFILE, "matched_topics": matched},
    }


def _project_evidence(item: dict[str, Any], matched: list[str]) -> dict[str, Any]:
    source = item["source"]
    anchor = source["message_anchor"]
    return {
        "evidence_reference_id": item["candidate_id"],
        "evidence_ids": deepcopy(item["evidence_ids"]),
        "conversation_id": source["conversation"]["conversation_id"],
        "message_anchor": {
            "message_id": anchor["message_id"],
            "role": anchor["role"],
            "time": anchor["time"],
        },
        "reference_uri": source["reference_uri"],
        "hit_types": sorted({hit["hit_type"] for hit in item["hit_provenance"]}),
        "relevance": {"profile": RELEVANCE_PROFILE, "matched_topics": matched},
        "expanded_excerpt": None,
    }


def _render_model_context(task: dict[str, Any], profile: dict[str, Any], candidates: dict[str, Any], evidence: dict[str, Any]) -> str:
    current = task["current_retrieval_results"]
    lines = [
        f"YunOS minimal context {PROTOCOL_VERSION}",
        "[A CURRENT TASK]",
        f"user_question: {task['current_user_question']}",
        "query_topics: " + (", ".join(task["query_topics"]) or "none"),
        f"time_range: {task['time_range']['start_time'] or 'none'} .. {task['time_range']['end_time'] or 'none'}",
        f"current_retrieval: {current['status']} ({current['reason_code']}); degradation={current['degradation']['status']}; missing={','.join(current['missing_capabilities']) or 'none'}; source_selected={current['source_selected_count']}; task_relevant={current['task_relevant_count']}",
        "assembled_references: " + (", ".join(current["assembled_reference_ids"]) or "none"),
    ]
    if current["warnings"]:
        lines.append("source_warnings: " + " | ".join(
            f"{item['code']}={item['message']}" for item in current["warnings"]
        ))
    else:
        lines.append("source_warnings: none")
    lines.append("[B CONFIRMED USER PROFILE - APPROVED ONLY]")
    if profile["items"]:
        lines.extend(
            f"- [approved/{item['kind']}/{item['profile_item_id']}] {item['statement']}"
            for item in profile["items"]
        )
    else:
        lines.append("- none")
    lines.append("[C CANDIDATE MEMORY - AUXILIARY, NOT FACT]")
    if candidates["items"]:
        lines.extend(
            f"- [candidate/not_approved/{item['candidate_type']}/{item['candidate_id']}] {item['statement']} | {item['reference_uri']}"
            for item in candidates["items"]
        )
    else:
        lines.append("- none")
    for relation in candidates["relations"]:
        lines.append(
            f"- [candidate_relation/{relation['relation']}/{relation['relation_id']}] "
            f"{relation['newer_candidate_id']} -> {relation['older_candidate_id']}"
        )
    lines.append("[D HISTORICAL EVIDENCE - CITATION/ANCHOR DEFAULT]")
    if evidence["items"]:
        for item in evidence["items"]:
            anchor = item["message_anchor"]
            lines.append(
                f"- [{item['evidence_reference_id']}] {item['conversation_id']}/{anchor['message_id']} "
                f"{anchor['role']} {anchor['time']} | {item['reference_uri']}"
            )
            if item["expanded_excerpt"] is not None:
                lines.append(f"  expanded_excerpt: {item['expanded_excerpt']}")
    else:
        lines.append("- none")
    return "\n".join(lines) + "\n"


def _topic_terms(text: str, *, allow_fallback: bool) -> list[str]:
    if not isinstance(text, str):
        raise ContextAssemblyValidationError("topic source must be text")
    normalized = unicodedata.normalize("NFKC", text).casefold()
    raw = [token for token in _LATIN_TOKEN_RE.findall(normalized) if len(token) >= 2]
    raw.extend(segment for segment in _HAN_SEGMENT_RE.findall(normalized) if 2 <= len(segment) <= 12)
    terms = sorted({term for term in raw if term not in _STOP_TERMS})
    if not terms and allow_fallback:
        terms = sorted(set(raw))
    return terms[:32]


def _matched_topics(topics: list[str], text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    latin_tokens = set(_LATIN_TOKEN_RE.findall(normalized))
    han_segments = _HAN_SEGMENT_RE.findall(normalized)
    matched = []
    for term in topics:
        if _HAN_SEGMENT_RE.fullmatch(term):
            if term in han_segments or (
                len(term) >= 4 and any(term in segment for segment in han_segments)
            ):
                matched.append(term)
        elif term in latin_tokens:
            matched.append(term)
    return matched


def _relevance_sort_key(matched: list[str], item_id: str) -> tuple[Any, ...]:
    return (-len(matched), item_id)


def _candidate_sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
    observed = datetime.fromisoformat(item["observed_at_utc"].replace("Z", "+00:00")).timestamp()
    return (
        -len(item["relevance"]["matched_topics"]),
        0 if item["confidence"] == "high" else 1,
        0 if item["review_status"] == "ready_for_review" else 1,
        -observed,
        item["candidate_id"],
    )


def _omission(layer: str, item_type: str, item_id: str, reason: str, priority: int) -> dict[str, Any]:
    if reason not in OMISSION_REASONS:
        raise ContextAssemblyValidationError(f"unsupported omission reason: {reason}")
    return {"layer": layer, "item_type": item_type, "item_id": item_id, "reason": reason, "priority": priority}


def _assembly_status(selection: dict[str, Any], temporal: dict[str, Any], profile: dict[str, Any], candidates: dict[str, Any], evidence: dict[str, Any]) -> tuple[str, str]:
    if selection["status"] == "unavailable":
        return "unavailable", selection["reason_code"]
    if temporal["status"] == "unavailable":
        return "unavailable", temporal["reason_code"]
    if selection["status"] == "partial" or temporal["status"] == "partial":
        return "partial", "partial_source_context"
    if (
        selection["degradation"]["status"] != "complete"
        or selection["missing_capabilities"]
        or selection["warnings"]
    ):
        return "partial", "degraded_source_context"
    if not profile["items"] and not candidates["items"] and not evidence["items"]:
        return "ok", "current_task_only"
    return "ok", "assembly_complete"


def _validate_policy(policy: Any) -> dict[str, Any]:
    _exact_keys(policy, {
        "policy_version", "policy_identity", "budget_profile", "limits", "priority_order",
        "relevance_profile", "history_default", "semantic_truncation", "token_estimator",
        "source_selection",
    }, "selection policy")
    if policy["policy_version"] != SELECTION_POLICY_VERSION:
        raise ContextAssemblyValidationError("selection policy version mismatch")
    limits = policy["limits"]
    _exact_keys(limits, {
        "total_characters", "estimated_tokens", "max_evidence", "max_candidates",
        "max_history_expansions",
    }, "selection limits")
    builtin = next(
        (item for item in BUILTIN_PROFILES.values() if item.name == policy["budget_profile"]),
        None,
    )
    if builtin is not None:
        expected_limits = {
            "total_characters": builtin.total_characters,
            "estimated_tokens": builtin.estimated_tokens,
            "max_evidence": builtin.max_evidence,
            "max_candidates": builtin.max_candidates,
            "max_history_expansions": builtin.max_history_expansions,
        }
        if limits != expected_limits:
            raise ContextAssemblyValidationError("built-in selection policy mismatch")
    elif policy["budget_profile"] == "Custom":
        for name, value in limits.items():
            low, high = CUSTOM_LIMITS[name]
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ContextAssemblyValidationError(f"invalid Custom {name}")
    else:
        raise ContextAssemblyValidationError("invalid assembly budget profile")
    fixed = deepcopy(policy)
    identity = fixed.pop("policy_identity")
    if identity != "csp_" + _digest(fixed):
        raise ContextAssemblyValidationError("selection policy identity is unstable")
    if (
        policy["priority_order"] != ["current_task", "confirmed_user_profile", "candidate_memory", "historical_evidence"]
        or policy["relevance_profile"] != RELEVANCE_PROFILE
        or policy["history_default"] != "citation_and_anchor_only"
        or policy["semantic_truncation"] is not False
        or policy["token_estimator"] != {
            "profile": ESTIMATOR_PROFILE,
            "method": "ceil(unicode_code_points/4)",
            "exact_model_tokenizer": False,
        }
        or policy["source_selection"] != {"protocol": "v7.1", "budget_profile": SOURCE_SELECTION_PROFILE}
    ):
        raise ContextAssemblyValidationError("selection policy rules mismatch")
    return policy


def _validate_approved_profile_item(item: Any) -> dict[str, Any]:
    _exact_keys(item, {
        "schema_version", "profile_item_id", "kind", "statement", "topics", "approval",
    }, "approved profile item")
    if item["schema_version"] != APPROVED_PROFILE_SCHEMA or item["kind"] not in PROFILE_KINDS:
        raise ContextAssemblyValidationError("invalid approved profile schema or kind")
    if not isinstance(item["statement"], str) or not 1 <= len(item["statement"]) <= 500:
        raise ContextAssemblyValidationError("invalid approved profile statement")
    if not isinstance(item["topics"], list) or not item["topics"] or item["topics"] != sorted(set(item["topics"])):
        raise ContextAssemblyValidationError("approved profile topics must be canonical and non-empty")
    if any(term not in _topic_terms(" ".join(item["topics"]), allow_fallback=True) for term in item["topics"]):
        raise ContextAssemblyValidationError("approved profile topics are not normalized")
    _exact_keys(item["approval"], {"status", "authority", "approval_id", "approved_at"}, "profile approval")
    approval = item["approval"]
    if approval["status"] != "approved" or approval["authority"] != "user":
        raise ContextAssemblyValidationError("only user-approved profile items are accepted")
    if not isinstance(approval["approval_id"], str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,100}", approval["approval_id"]):
        raise ContextAssemblyValidationError("invalid profile approval identity")
    try:
        parsed = datetime.fromisoformat(approval["approved_at"].replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ContextAssemblyValidationError("invalid profile approval time") from exc
    if parsed.tzinfo is None:
        raise ContextAssemblyValidationError("profile approval time must include timezone")
    expected = profile_item_identity(item)
    if item["profile_item_id"] != expected:
        raise ContextAssemblyValidationError("approved profile identity mismatch")
    try:
        _reject_context_absolute_paths(item)
    except MemoryPackageValidationError as exc:
        raise ContextAssemblyValidationError(
            "approved profile item violates the v7.0 absolute-path boundary"
        ) from exc
    return item


def _validate_source_chain(value: Any) -> None:
    _exact_keys(value, {"retrieval", "memory_package", "evidence_selection", "temporal_candidates"}, "source chain")
    _exact_keys(value["retrieval"], {"protocol_version", "status", "reason_code"}, "retrieval source")
    _exact_keys(value["memory_package"], {"protocol_version", "package_identity", "status", "reason_code"}, "memory_package source")
    _exact_keys(value["evidence_selection"], {
        "protocol_version", "package_identity", "status", "reason_code", "capabilities",
        "degradation", "missing_capabilities", "warnings",
    }, "evidence_selection source")
    _exact_keys(value["temporal_candidates"], {"protocol_version", "package_identity", "status", "reason_code", "candidate_only"}, "temporal source")
    if value["temporal_candidates"]["candidate_only"] is not True:
        raise ContextAssemblyValidationError("v7.2 source must remain candidate-only")
    expected_protocols = {
        "retrieval": RETRIEVAL_PROTOCOL_VERSION,
        "memory_package": MEMORY_PACKAGE_PROTOCOL_VERSION,
        "evidence_selection": EVIDENCE_SELECTION_PROTOCOL_VERSION,
        "temporal_candidates": TEMPORAL_CANDIDATE_PROTOCOL_VERSION,
    }
    for name, version in expected_protocols.items():
        if value[name]["protocol_version"] != version:
            raise ContextAssemblyValidationError(f"{name} protocol version mismatch")
    patterns = {"memory_package": r"mp_[0-9a-f]{64}", "evidence_selection": r"msp_[0-9a-f]{64}", "temporal_candidates": r"tmc_[0-9a-f]{64}"}
    for name, pattern in patterns.items():
        if not re.fullmatch(pattern, value[name]["package_identity"]):
            raise ContextAssemblyValidationError(f"invalid {name} identity")


def _validate_task_context(value: Any) -> None:
    _exact_keys(value, {"current_user_question", "query_topics", "time_range", "current_retrieval_results"}, "task context")
    if not isinstance(value["current_user_question"], str) or not value["current_user_question"]:
        raise ContextAssemblyValidationError("current task question is required")
    if value["query_topics"] != _topic_terms(value["current_user_question"], allow_fallback=True):
        raise ContextAssemblyValidationError("query topics do not match the current task")
    _exact_keys(value["time_range"], {"start_time", "end_time"}, "task time range")
    current = value["current_retrieval_results"]
    _exact_keys(current, {
        "status", "reason_code", "capabilities", "degradation", "missing_capabilities",
        "warnings", "source_selected_count", "task_relevant_count", "assembled_reference_ids",
    }, "current retrieval results")
    if any(isinstance(current[name], bool) or not isinstance(current[name], int) or current[name] < 0 for name in ("source_selected_count", "task_relevant_count")):
        raise ContextAssemblyValidationError("invalid current retrieval counts")
    if not isinstance(current["assembled_reference_ids"], list) or len(current["assembled_reference_ids"]) != len(set(current["assembled_reference_ids"])):
        raise ContextAssemblyValidationError("invalid assembled retrieval references")


def _validate_layers(package: dict[str, Any]) -> None:
    profile = package["confirmed_user_profile"]
    _exact_keys(profile, {"authority", "production_input", "candidate_promotion_allowed", "items"}, "confirmed profile layer")
    if profile["authority"] != "independently_supplied_approved_profile" or profile["production_input"] != "none" or profile["candidate_promotion_allowed"] is not False:
        raise ContextAssemblyValidationError("confirmed profile trust boundary mismatch")
    for item in profile["items"]:
        _exact_keys(item, {"schema_version", "profile_item_id", "kind", "statement", "topics", "approval", "relevance"}, "assembled profile item")
        base = {k: deepcopy(v) for k, v in item.items() if k != "relevance"}
        _validate_approved_profile_item(base)
        _validate_relevance(item["relevance"], package["task_context"]["query_topics"])

    candidates = package["candidate_memory"]
    _exact_keys(candidates, {"candidate_only", "may_be_used_as_auxiliary_reference", "items", "relations"}, "candidate layer")
    if candidates["candidate_only"] is not True or candidates["may_be_used_as_auxiliary_reference"] is not True:
        raise ContextAssemblyValidationError("candidate layer trust boundary mismatch")
    candidate_ids = set()
    for item in candidates["items"]:
        _exact_keys(item, {"candidate_id", "state", "approval_status", "candidate_type", "statement", "review_status", "confidence", "observed_at_utc", "source_candidate_id", "reference_uri", "relevance"}, "assembled candidate")
        if item["state"] != "candidate" or item["approval_status"] != "not_approved":
            raise ContextAssemblyValidationError("candidate cannot masquerade as an approved fact")
        if not re.fullmatch(r"mc_[0-9a-f]{64}", item["candidate_id"]) or item["candidate_id"] in candidate_ids:
            raise ContextAssemblyValidationError("invalid or duplicate candidate identity")
        candidate_ids.add(item["candidate_id"])
        _validate_relevance(item["relevance"], package["task_context"]["query_topics"])
    relation_ids = set()
    for relation in candidates["relations"]:
        if set(relation) != {"relation_id", "newer_candidate_id", "older_candidate_id", "relation", "exclusive", "reason", "evidence_event_ids"}:
            raise ContextAssemblyValidationError("candidate relation fields mismatch")
        if not {relation["newer_candidate_id"], relation["older_candidate_id"]} <= candidate_ids:
            raise ContextAssemblyValidationError("candidate relation endpoint is absent")
        if relation["relation_id"] in relation_ids:
            raise ContextAssemblyValidationError("duplicate relation identity")
        relation_ids.add(relation["relation_id"])

    evidence = package["historical_evidence"]
    _exact_keys(evidence, {"default_representation", "items", "expanded_count"}, "historical evidence layer")
    if evidence["default_representation"] != "citation_and_anchor_only":
        raise ContextAssemblyValidationError("historical evidence must default to anchors")
    evidence_ids = []
    expanded = 0
    for item in evidence["items"]:
        _exact_keys(item, {"evidence_reference_id", "evidence_ids", "conversation_id", "message_anchor", "reference_uri", "hit_types", "relevance", "expanded_excerpt"}, "historical evidence")
        _exact_keys(item["message_anchor"], {"message_id", "role", "time"}, "historical anchor")
        if item["expanded_excerpt"] is not None and (not isinstance(item["expanded_excerpt"], str) or not item["expanded_excerpt"]):
            raise ContextAssemblyValidationError("history expansion must be a complete non-empty excerpt")
        expanded += item["expanded_excerpt"] is not None
        evidence_ids.append(item["evidence_reference_id"])
        _validate_relevance(item["relevance"], package["task_context"]["query_topics"])
    if len(evidence_ids) != len(set(evidence_ids)) or evidence["expanded_count"] != expanded:
        raise ContextAssemblyValidationError("historical evidence counts mismatch")
    if package["task_context"]["current_retrieval_results"]["assembled_reference_ids"] != evidence_ids:
        raise ContextAssemblyValidationError("current retrieval references do not match historical evidence")


def _validate_relevance(value: Any, task_topics: list[str]) -> None:
    _exact_keys(value, {"profile", "matched_topics"}, "relevance")
    if value["profile"] != RELEVANCE_PROFILE or not value["matched_topics"]:
        raise ContextAssemblyValidationError("included memory must be task relevant")
    if value["matched_topics"] != [term for term in task_topics if term in value["matched_topics"]]:
        raise ContextAssemblyValidationError("matched topics are not in task order")


def _validate_budget(package: dict[str, Any], policy: dict[str, Any]) -> None:
    budget = package["budget"]
    _exact_keys(budget, {"total_characters_limit", "used_characters", "remaining_characters", "character_hard_limit_satisfied", "estimated_token_limit", "estimated_tokens_used", "estimated_tokens_remaining", "estimator_profile", "exact_model_tokenizer"}, "assembly budget")
    limits = policy["limits"]
    expected = {
        "total_characters_limit": limits["total_characters"],
        "used_characters": len(package["model_context"]),
        "remaining_characters": limits["total_characters"] - len(package["model_context"]),
        "character_hard_limit_satisfied": len(package["model_context"]) <= limits["total_characters"],
        "estimated_token_limit": limits["estimated_tokens"],
        "estimated_tokens_used": estimate_tokens(package["model_context"]),
        "estimated_tokens_remaining": limits["estimated_tokens"] - estimate_tokens(package["model_context"]),
        "estimator_profile": ESTIMATOR_PROFILE,
        "exact_model_tokenizer": False,
    }
    if budget != expected or not budget["character_hard_limit_satisfied"] or budget["estimated_tokens_remaining"] < 0:
        raise ContextAssemblyValidationError("assembly budget mismatch")
    if len(package["candidate_memory"]["items"]) > limits["max_candidates"] or len(package["historical_evidence"]["items"]) > limits["max_evidence"] or package["historical_evidence"]["expanded_count"] > limits["max_history_expansions"]:
        raise ContextAssemblyValidationError("assembly count limit exceeded")


def _validate_omissions_counts_and_transparency(package: dict[str, Any]) -> None:
    omitted = package["omitted"]
    if not isinstance(omitted, list) or omitted != sorted(omitted, key=lambda item: (item["priority"], item["layer"], item["item_type"], item["item_id"], item["reason"])):
        raise ContextAssemblyValidationError("omitted manifest is not canonical")
    for item in omitted:
        _exact_keys(item, {"layer", "item_type", "item_id", "reason", "priority"}, "omitted item")
        if item["reason"] not in OMISSION_REASONS or not isinstance(item["priority"], int):
            raise ContextAssemblyValidationError("invalid omission reason or priority")
    counts = package["counts"]
    _exact_keys(counts, {"approved_profile_used", "candidates_used", "candidate_relations_used", "evidence_used", "history_expanded", "omitted", "omission_reasons"}, "assembly counts")
    expected = {
        "approved_profile_used": len(package["confirmed_user_profile"]["items"]),
        "candidates_used": len(package["candidate_memory"]["items"]),
        "candidate_relations_used": len(package["candidate_memory"]["relations"]),
        "evidence_used": len(package["historical_evidence"]["items"]),
        "history_expanded": package["historical_evidence"]["expanded_count"],
        "omitted": len(omitted),
        "omission_reasons": {name: sum(item["reason"] == name for item in omitted) for name in OMISSION_REASONS},
    }
    if counts != expected:
        raise ContextAssemblyValidationError("assembly counts mismatch")
    evidence_omissions = [
        item for item in omitted
        if item["layer"] == "historical_evidence" and item["item_type"] == "evidence"
    ]
    relevant_omissions = sum(item["reason"] != "irrelevant_to_task" for item in evidence_omissions)
    irrelevant_omissions = sum(item["reason"] == "irrelevant_to_task" for item in evidence_omissions)
    current = package["task_context"]["current_retrieval_results"]
    if current["task_relevant_count"] != counts["evidence_used"] + relevant_omissions:
        raise ContextAssemblyValidationError("task-relevant retrieval count mismatch")
    if current["source_selected_count"] != current["task_relevant_count"] + irrelevant_omissions:
        raise ContextAssemblyValidationError("source-selected retrieval count mismatch")
    transparency = package["transparency"]
    _exact_keys(transparency, {"description", "model_saw", "excluded", "usage"}, "transparency")
    if transparency["description"] != "本次回答模型看到了什么" or transparency["excluded"] != omitted or transparency["usage"] != package["budget"]:
        raise ContextAssemblyValidationError("transparency manifest mismatch")
    expected_saw = {
        "approved_profile_ids": [item["profile_item_id"] for item in package["confirmed_user_profile"]["items"]],
        "candidate_ids": [item["candidate_id"] for item in package["candidate_memory"]["items"]],
        "evidence_reference_ids": [item["evidence_reference_id"] for item in package["historical_evidence"]["items"]],
        "expanded_evidence_ids": [item["evidence_reference_id"] for item in package["historical_evidence"]["items"] if item["expanded_excerpt"] is not None],
    }
    if transparency["model_saw"] != expected_saw:
        raise ContextAssemblyValidationError("model-saw manifest mismatch")


def _identity_payload(package: dict[str, Any]) -> dict[str, Any]:
    return {key: deepcopy(value) for key, value in package.items() if key != "assembly_identity"}


def _exact_keys(value: Any, expected: set[str], label: str) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise ContextAssemblyValidationError(f"{label} fields mismatch")
